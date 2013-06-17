"""
failnozzle:

    A daemon for batching error log messages, emailing digests, and alerting on
    error rates.
"""
# even though this violates pep8 import order, it has to be here to
# monkey patch before any potentially blocking modules are imported.
import gevent
import gevent.monkey
gevent.monkey.patch_all()

from collections import defaultdict, namedtuple
from contextlib import contextmanager
from datetime import datetime
from email.mime.text import MIMEText
import atexit
import json
import logging
import os
import smtplib
import sys

from jinja2 import Environment, FileSystemLoader
import gevent.coros
import gevent.queue
import gevent.socket

from failnozzle import settings

# Pylint doesn't grasp gevent and socket.
# pylint: disable=E1101


_SENTINEL = object()


def setting(name, default=_SENTINEL):
    """
    Return a setting value or a default if it is not present.

    Note that, unlike many getattr-like functions, if the caller does *not*
    provide a default, and the setting is not found, this function will raise
    an Exception.  That allows the code in this module to provide empty default
    values for certain settings -- so that unit testing doesn't require full
    settings.  But, at the same time, for all other settings, if one is
    missing, the code will fail loudly and specifically.
    """
    if hasattr(settings, name):
        return getattr(settings, name)

    if default != _SENTINEL:
        return default

    raise Exception("Couldn't find setting %s" % name)


class MessageBuffer(object):
    """
    Stores and organizes incoming messages by their source (the host that
    produced them) and kind (the application that produced them) in a
    concurrency-safe way. Can be flushed to produce a report about the messages
    it's seen before forgetting those messages.
    """
    def __init__(self, subject_template, body_template):
        self.counts_by_unique = defaultdict(MessageCounts)
        self.lock = gevent.coros.Semaphore()
        self.subject_template = subject_template
        self.body_template = body_template

    @contextmanager
    def locked(self):
        """
        A context manager for locking (when manipulating shared data like
        counts_by_unique).
        """
        self.lock.acquire()
        try:
            yield
        finally:
            self.lock.release()

    def add(self, unique_message, source):
        """
        Adds an occurrance of a unique message from `source`.
        """
        with self.locked():
            self.counts_by_unique[unique_message].increment(source)

    def total_matching(self, pred):
        """
        Computes the total number of received messages in the buffer
        that match the predicate.
        """
        return sum([counts.total
                    for (uniq_msg, counts)
                    in self.counts_by_unique.items()
                    if pred(uniq_msg)])

    @property
    def total(self):
        """
        Return the total count of all messages in the buffer.
        """
        return self.total_matching(lambda um: True)

    @property
    def total_unique(self):
        """
        Computes the total number of _unique_ received messages in the buffer.
        """
        return len(self.counts_by_unique)

    @property
    def unique_messages(self):
        """
        Get the unique messages held by this buffer.
        """
        return self.counts_by_unique.keys()

    @property
    def kinds(self):
        """
        Returns a set of the unique kinds of messages in the buffer.
        """
        return set(unique.kind for unique in self.counts_by_unique.iterkeys())

    @property
    def sorted_counts(self):
        """
        Returns a list of pairs of unique message and count, sorted in reverse
        order of count.
        """
        return sorted(self.counts_by_unique.items(),
                      key=lambda (_, counts): counts.total,
                      reverse=True)

    def flush(self):
        """
        Flushes the buffer. This returns the subject line and body of a report
        about the contents of the buffer, then removes all messages from the
        buffer.
        """
        subject = None
        report = None
        unique_messages = []
        with self.locked():
            # here we want the whole count, not just the
            # messages/errors that are pageable, since we want to
            # report even on just monitoring errors...
            total = self.total
            if total > 0:
                try:
                    params = dict(server_name=setting('SERVER_NAME'),
                                  total=total,
                                  total_unique=self.total_unique,
                                  sorted_counts=self.sorted_counts,
                                  kinds=self.kinds)
                    unique_messages = self.unique_messages
                    subject = self.subject_template.render(params)
                    report = self.body_template.render(params)
                # Too general an exception but we want to make sure we recover
                # cleanly.
                # pylint: disable=W0703
                except Exception, exc:
                    # pylint: disable=E1205
                    logging.exception('Could not render report', exc)

            self.counts_by_unique.clear()
            return subject, report, unique_messages


class MessageCounts(object):
    """
    Tracks the number of a times a unique incoming message was received, by its
    source. Also maintains the first and last seen date of the message.
    """
    def __init__(self):
        self.sources = defaultdict(int)
        self.first_seen = None
        self.last_seen = None

    def increment(self, source):
        """
        Increments the count of the message for `source`, and updates the first
        and last seen dates accordingly.
        """
        self.sources[source] += 1
        now = datetime.now()
        if self.first_seen is None:
            self.first_seen = now
        self.last_seen = now

    @property
    def total(self):
        """
        Computes the total number of times this message was seen, across all
        sources.
        """
        return sum(self.sources.values())

    @property
    def sources_sorted(self):
        """
        Returns a list of pairs of source name and count, sorted by the source
        name.
        """
        return sorted(self.sources.items())


class MessageRate(object):
    """
    Tracks the rate of incoming messages, determining whether the number of
    messages received within a window exceeds a threshold.
    """
    def __init__(self, window, limit):
        self.window = window
        self.limit = limit
        self.counts = []

    def add_and_check(self, count):
        """
        Adds a message count, sliding the window if necessary.
        """
        if len(self.counts) >= self.window:
            self.counts.pop(0)
        self.counts.append(count)
        total = sum(self.counts)
        if total >= self.limit:
            return True, total
        else:
            return False, total

    def reset(self):
        """
        Resets the recorded counts.
        """
        self.counts = []


# namedtuples are class-like in usage, so ignore Pylint's objection
# pylint: disable=C0103
UniqueMessage = namedtuple('UniqueMessage',
                           ['module', 'funcName', 'filename', 'message',
                            'pathname', 'lineno', 'exc_text', 'kind'])
# pylint: enable=C0103


def _get_unique_msg_tuple():
    """
    Gets the namedtuple to use to represent a unique message from
    the UNIQUE_MSG_TUPLE setting.

    If not specified will use failnozzle.server.UniqueMessage.
    """
    return setting('UNIQUE_MSG_TUPLE', UniqueMessage)


def processor(message_queue, message_buffer):
    """
    Processes incoming messages from a queue, adding them to a MessageBuffer
    and tracking their rate with a MessageRate.
    """
    while True:
        try:
            _process_one_message(message_queue, message_buffer)

        # We want to catch everything.
        # pylint: disable=W0702
        except:
            logging.error(
                "Unhandled exception while processing message, "
                "will attempt to log")
            try:
                # in case something is being pickled / unpickled even
                # at this level, protect against error while logging
                # the exception.
                logging.exception("Unhandled exception details")

            # Again, want to catch everything.
            # pylint: disable=W0702
            except:
                # ok, we give.
                logging.error(
                    "Could not log unhandled exception safely, sorry.")


def _process_one_message(message_queue, message_buffer):
    """
    Try to pull / process a single message from the queue.
    """
    # Get the next message from the queue.
    next_message = message_queue.get()
    logging.debug('Processing incoming message')

    # Extract the fields to dedupe over into a UniqueMessage.
    source = next_message.get(setting('SOURCE_FIELD_NAME'), None)

    # We want to extract only fields that exist in UniqueMessage.
    # pylint: disable=W0212
    message_params = {k: v for k, v in next_message.items()
                      if k in _get_unique_msg_tuple()._fields}

    # If the message for this log entry spans multiple lines clip it at the
    # first.
    msg_str = message_params.get('message')
    if msg_str and '\n' in msg_str:
        if not message_params.get('exc_text'):
            message_params['exc_text'] = msg_str
        msg_str = msg_str[:msg_str.index('\n')]
        message_params['message'] = msg_str

    unique = _package_unique_message(message_params)

    message_buffer.add(unique, source)
    logging.debug('Done processing incoming message')


def _package_unique_message(message_params):
    """
    Safely package message_params as a UniqueMessage, ensuring that
    all fields are present and accounted for.
    """
    unique_message_impl = _get_unique_msg_tuple()
    return unique_message_impl(**_ensure_message_params(message_params))


def _ensure_message_params(message_params):
    """
    Ensure that the message parameters we are given are a super set of the
    supported message parameters. If any parameters are missing, fill in None
    for that key.
    """
    all_params = dict(message_params)

    # We want to make sure we got all fields, or fill in with None.
    # pylint: disable=W0212
    for field in _get_unique_msg_tuple()._fields:
        if field not in all_params:
            logging.warn(
                "No specified field for %s, using None", field)
            all_params[field] = None

    return all_params


def flush_trigger(*args):
    """
    Passes args through directly to a background greenlet that flushes the
    buffer periodically.
    """
    while True:
        gevent.sleep(seconds=setting('FLUSH_SECONDS'))
        logging.debug('Triggering flush')
        gevent.spawn(flusher, *args)


def flusher(message_buffer, message_rate):
    """
    Checks the incoming message rate, using a background pager
    greenlet if non-just-monitoring rate exceeded, and flushes the
    message buffer, using a background emailer greenlet to send the
    email.
    """
    join_greenlets = []

    # Check the message rate, not including "just monitoring" messages
    # in the message rate.  TODO: at some point, if this becomes more
    # complex, make it more config-y.
    total_matching = message_buffer.total_matching(
        is_not_just_monitoring_error)
    logging.debug("Found %d non-monitoring messages, %d total",
                  total_matching, message_buffer.total)
    exceeded, total = message_rate.add_and_check(total_matching)

    if exceeded:
        logging.debug('Flusher is sending a page')
        join_greenlets.append(gevent.spawn(pager, total))
        message_rate.reset()
    else:
        logging.debug('Flusher is NOT sending a page')

    # Flush the buffer and email a report.
    subject, report, unique_messages = message_buffer.flush()

    recips = calc_recips(unique_messages)
    logging.debug("Calculated recips = %s", recips)

    if report:
        logging.debug('Flusher is sending a report')
        join_greenlets.append(gevent.spawn(mailer, recips, subject, report))
    else:
        logging.debug('Flusher is NOT sending a report')

    # Wait for the pager & mailer. This is for atexit, so we don't actually
    # exit until these have both had a chance to finish.
    if join_greenlets:
        gevent.joinall(join_greenlets)


def is_just_monitoring_error(unique_message):
    """
    Return True if the unique_message is an intentional error just for
    monitoring (meaning that it contains the one of the
    JUST_MONITORING_ERROR_MARKERS somewhere in the exc_text)
    """
    return any([(marker in unicode(unique_message.exc_text) or
                 marker in unicode(unique_message.message))
                for marker
                in setting('MONITORING_ERROR_MARKERS')])


def is_not_just_monitoring_error(unique_message):
    """
    Return True if unique_message does not appear to be an intentional
    error.
    """
    return not is_just_monitoring_error(unique_message)


def deferred_setting(name, default):
    """
    Returns a function that calls settings with (name, default)
    """
    return lambda: setting(name, default)

# patterns for figuring out which recipients should be added to an
# error summary.  Note that we're not attempting to split out
# different errors into different emails--if a recipient matches *any*
# error in a flushed batch, the recipient is added to the email, and
# will see all the errors.  The form of the list is:
# [(email_recipient_addr, callable_with_unique_message), ...]  where
# the callable_with_unique_message takes a UniqueMessage and returns
# True if the presence of the message should be alerted to the
# corresponding recipient.
RECIP_MATCHERS = [
    (deferred_setting('JUST_MONITORING_REPORT_TO', ''),
        is_just_monitoring_error),
    (deferred_setting('REPORT_TO', ''),
        is_not_just_monitoring_error)
]


def _get_recip_matchers():
    """
    Return an iterator over recipient matchers, handling any deferred
    settings
    """
    for recip, matcher in RECIP_MATCHERS:
        if callable(recip):
            recip_value = recip()
        else:
            recip_value = recip

        yield recip_value, matcher


def calc_recips(unique_messages):
    """
    Calculate all recipients for the error summary, based on the kinds
    of errors that are batched up to go out, and return them as a
    list.

    Note that based on the configuration of we *shouldn't* ever return
    an empty list, but this is not promised, so callers should take
    steps to make sure there is a default recipient if one is not
    found here.
    """
    recips = set()
    for unique_message in unique_messages:
        for recip, recip_matcher in _get_recip_matchers():
            if recip_matcher(unique_message):
                logging.debug("Matched against %s adding recip %s",
                              recip_matcher,
                              recip)
                recips.add(recip)
    return list(recips)


def mailer(recips, subject, report):
    """
    Sends an email containing a report from a flushed MessageBuffer.
    """
    if not recips:
        logging.error("Recips was empty, adding error recip")
        recips.append(setting('REPORT_TO', ''))
    logging.info('Mailer is emailing, subject = %r, recipients=%r',
                 subject, recips)
    send_email(setting('REPORT_FROM', ''), ', '.join(recips),
               subject, report, reply_to=setting('REPLY_TO', ''))


def pager(total):
    """
    Sends an email to the pager, alerting us of the total number of messages
    that it's received within the window.
    """
    logging.info('Pager is emailing, count = %r', total)
    report = u'Danger: received %d errors within the alert window.' % total
    send_email(setting('PAGER_FROM'), setting('PAGER_TO'),
               '%s error rate exceeded' % setting('SERVER_NAME'), report,
               reply_to=setting('PAGER_REPLY_TO', ''))


def send_email(from_addr, to_addr, subject, body, reply_to=None):
    """
    Sends a text/plain email from `from_addr` to the address `to_addr`, with
    subject `subject` and body `body`, using the host, port, user, and password
    from settings.
    """
    try:
        msg = MIMEText(body, 'plain')
        msg['From'] = from_addr
        msg['To'] = to_addr
        msg['Subject'] = subject
        if reply_to is not None:
            msg['Reply-To'] = reply_to

        smtp = smtplib.SMTP_SSL(setting('SMTP_HOST'), setting('SMTP_PORT'))
        smtp.login(setting('SMTP_USER'), setting('SMTP_PASSWORD'))
        smtp.sendmail(from_addr, [to_addr], msg.as_string())
        smtp.close()

    # Too general an exception but we want to make sure we recover/log
    # cleanly.
    # pylint: disable=W0703
    except Exception, exc:
        logging.exception('Error sending email "%s": %s', subject, exc)


def _create_queue_rate_buffer():
    """
    Setup our MessageQueue, MessageRate, and MessageBuffer.

    returns (message_queue, message_rate, message_buffer)
    """
    # Find the template directory, either our directory or user specified
    default_template_dir = os.path.dirname(__file__)
    template_dir = setting('EMAIL_TEMPLATE_DIR', default_template_dir)

    env = Environment(loader=FileSystemLoader(template_dir))

    subject_template = env.get_template(setting('EMAIL_SUBJECT_TEMPLATE'))
    body_template = env.get_template(setting('EMAIL_BODY_TEMPLATE'))

    message_buffer = MessageBuffer(subject_template, body_template)
    message_rate = MessageRate(setting('PAGER_WINDOW_SIZE'),
                               setting('PAGER_LIMIT'))

    message_queue = gevent.queue.Queue()
    return (message_queue, message_rate, message_buffer)


def _validate_settings():
    """
    Validate the settings to make sure we are sane.
    """
    # The below can be over-ridden to customize.  If they are, they must be
    # non-null.
    not_none_params = ['EMAIL_BODY_TEMPLATE',
                       'EMAIL_SUBJECT_TEMPLATE',
                       'EMAIL_TEMPLATE_DIR',
                       'INTERNAL_ERROR_FUNC',
                       'SOURCE_FIELD_NAME',
                       'UNIQUE_MSG_TUPLE']

    for param in not_none_params:
        val = setting(param, 'X')
        assert val is not None, 'Must specify a non-None value for %s' % param


def main():
    """
    Spawns greenlets for processing incoming messages, then listens for UDP
    packets, handing them off to those greenlets.
    """
    # Yes, friends, the log aggregator does some logging of its own.
    logging.basicConfig(level=setting('LOG_LEVEL'),
                        format=setting('LOG_FORMAT'))
    logging.info('Starting up')

    if len(sys.argv) > 1:
        settings.import_config_file(sys.argv[1])

    _validate_settings()

    # Setup our objects
    message_queue, message_rate, message_buffer = _create_queue_rate_buffer()

    # Start the message processor, and the loop that triggers flushing.
    gevent.spawn(processor, message_queue, message_buffer)
    gevent.spawn(flush_trigger, message_buffer, message_rate)

    # Ensure that we flush the buffer on exit no matter what.
    # Pylint thinks we never use this.
    # pylint: disable=W0612
    @atexit.register
    def flush_on_exit():
        """
        Ensures that the flusher runs when exiting.
        """
        flusher_greenlet = gevent.spawn(flusher, message_buffer, message_rate)
        flusher_greenlet.join()
    # pylint: enable=W0612

    # Create a socket to listen to incoming messages.
    socket = gevent.socket.socket(family=gevent.socket.AF_INET,
                                  type=gevent.socket.SOCK_DGRAM)
    socket.bind(setting('UDP_BIND'))
    logging.info('Listening on %r', setting('UDP_BIND'))

    # As messages arrive, unpack them and put them into the queue.
    count = 0
    while True:
        try:
            data = socket.recv(setting('INCOMING_MESSAGE_MAX_SIZE'))
            obj = json.loads(data)
            record = logging.makeLogRecord(obj)
            message_queue.put(vars(record))

        # Too general an exception but we want to make sure we recover
        # cleanly.
        # pylint: disable=W0703
        except Exception, exc:
            count += 1
            logging.exception('Error on incoming packet: %s', exc)
            message_queue.put(_make_fake_record(count, exc))


def _default_fake_record(count, exception):
    """
    Creates the default fake record
    """
    fake_record = dict(module='unknown',
                       funcName='unknown',
                       filename='unknown',
                       pathname='unknown',
                       lineno=0,
                       exc_text='Internal error: %d %r' % (count,
                                                           exception),
                       kind='unknown',
                       message='unknown')
    return fake_record


def _make_fake_record(count, exception):
    """
    Make a fake record, making sure that all UniqueMessage fields are
    filled in, so we don't get any untoward exceptions that break
    everything.
    """
    # Get the overridden function for creating a fake record or our default.
    fake_record_fn = setting('INTERNAL_ERROR_FUNC', _default_fake_record)

    return _ensure_message_params(fake_record_fn(count, exception))


if __name__ == '__main__':
    main()
