"""
Tests for the basic operation of the failnozzzle server
"""
from mock import ANY, call, DEFAULT, Mock, patch
from nose.tools import eq_
import os
import sys

from jinja2.environment import Environment
from jinja2.loaders import FileSystemLoader

from failnozzle import server
from failnozzle.server import calc_recips, flusher, is_just_monitoring_error, \
    mailer, MessageBuffer, MessageCounts, MessageRate, \
    _process_one_message, _package_unique_message, UniqueMessage, setting


# Fix path to import failnozzle
TEST_DIR = os.path.dirname(__file__)
sys.path.append(TEST_DIR + '/../..')


def test_message_buffer():
    subject_template = Mock()
    body_template = Mock()

    buf = MessageBuffer(subject_template, body_template)
    msg1 = UniqueMessage('test', 'test', 'test', 'message1', 'test.py', 1,
                         'exception text', 'app')
    msg2 = UniqueMessage('test', 'test', 'test', 'message2', 'test.py', 1,
                         'exception text', 'app')

    buf.add(msg1, 'host1')
    buf.add(msg1, 'host1')
    buf.add(msg1, 'host2')
    buf.add(msg2, 'host2')

    eq_(4, buf.total)
    eq_(2, buf.total_unique)
    eq_({'app'}, buf.kinds)
    eq_(2, len(buf.sorted_counts))
    eq_('message1', buf.sorted_counts[0][0].message)
    eq_(3, buf.sorted_counts[0][1].total)
    eq_(3, buf.total_matching(lambda m: m.message == 'message1'))

    subject_template.render.return_value = 'subj'
    body_template.render.return_value = 'body'

    subj, body, uniq_messages = buf.flush()
    eq_('subj', subj)
    eq_('body', body)
    eq_(2, len(uniq_messages))

    eq_(1, subject_template.render.call_count)
    eq_(4, body_template.render.call_args[0][0]['total'])
    eq_(2, body_template.render.call_args[0][0]['total_unique'])
    eq_(2, len(body_template.render.call_args[0][0]['sorted_counts']))
    eq_('message1',
        body_template.render.call_args[0][0]['sorted_counts'][0][0].message)
    eq_({'app'}, body_template.render.call_args[0][0]['kinds'])


def test_message_counts():
    counts = MessageCounts()
    counts.increment('host1')
    eq_(1, counts.total)
    for i in range(10):
        counts.increment('host2')

    eq_(11, counts.total)
    eq_(2, len(counts.sources_sorted))
    eq_(('host1', 1), counts.sources_sorted[0])
    eq_(('host2', 10), counts.sources_sorted[1])


def test_message_rate():
    rate = MessageRate(3, 3)
    eq_((False, 1), rate.add_and_check(1))
    eq_((False, 1), rate.add_and_check(0))
    eq_((False, 2), rate.add_and_check(1))
    eq_((False, 2), rate.add_and_check(1))
    eq_((True, 7), rate.add_and_check(5))
    rate.reset()
    eq_((False, 2), rate.add_and_check(2))


def test_is_just_monitoring_error():
    def check_is_just_monitoring_error(text, expected):
        "Verify the given message text is(n't) a monitoring error"
        msg1 = UniqueMessage('test', 'test', 'test', text, 'test.py', 1,
                             'exception text', 'app')
        msg2 = UniqueMessage('test', 'test', 'test', 'message text',
                             'test.py', 1, text, 'app')

        eq_(expected, is_just_monitoring_error(msg1))
        eq_(expected, is_just_monitoring_error(msg2))

    data = [
        ("It's 1669fe88-b0c3-439d-bc10-8a3d21493ede", True),
        ("Oh, and c84a3673-0a95-447b-810f-8107e1e38013 is good too", True),
        ("This is just a regular message", False),
    ]
    for text, expected in data:
        yield check_is_just_monitoring_error, text, expected


def test_calc_recips():
    monitoring_to = 'a@example.com'
    report_to = 'b@example.com'

    def check_calc_recips(msg, expected):
        "Patch the server's matchers and verify the patterns work as expected"
        with patch('failnozzle.server.RECIP_MATCHERS',
                   new=list(server.RECIP_MATCHERS)) as matchers:
            for i, matcher in enumerate(matchers):
                if matcher[1].__name__ == 'is_just_monitoring_error':
                    matchers[i] = (monitoring_to, matcher[1])
                else:
                    matchers[i] = (report_to, matcher[1])

            eq_([expected], calc_recips([msg]))

    data = [
        ("Oho, 1669fe88-b0c3-439d-bc10-8a3d21493ede", monitoring_to),
        ("This is a real actual error (sorta)", report_to),
    ]
    for msg_text, expected in data:
        msg = UniqueMessage('test', 'test', 'test', 'message text',
                            'test.py', 1, msg_text, 'app')
        yield check_calc_recips, msg, expected


def test_process_one_message():
    """
    Test we can process one message.
    """
    # Build our expected message
    message = {'module': 'log',
               'funcName': 'log_exception',
               'message': 'GET http://localhost:5000/folders/5/emails',
               'filename': 'log.py',
               'lineno': 214, 'args': [],
               'exc_text': 'Traceback (most recent call last):',
               'kind': 'app',
               'pathname': '/some/path.py',
               'source': 'eric-desktop'}

    message_params = {k: v for k, v in message.items()
                      # Intentionally ignoring Pylint's reservations about
                      # accessing private members here
                      # pylint: disable=W0212
                      if k in UniqueMessage._fields}
                      # pylint: enable=W0212
    expected_message = _package_unique_message(message_params)

    # Set up mocks for the queue and buffer.
    message_queue = Mock()
    message_queue.get.return_value = message
    message_buffer = Mock()

    _process_one_message(message_queue, message_buffer)

    # Make sure we sent this message to the buffer as expected.
    message_buffer.add.assert_called_once_with(expected_message,
                                               message['source'])


def test_process_one_multiline():
    """
    Test we gracefully handle a message where the message file is split
    over multiple lines. This means the stack trace is also the message.
    We want to clip it so the message is only the first line of the
    message.
    """
    # Build our expected message
    message = {'module': 'log',
               'funcName': 'log_exception',
               'message': '1\n2\n3\n',
               'filename': 'log.py',
               'lineno': 214, 'args': [],
               'exc_text': 'Traceback (most recent call last):',
               'kind': 'app',
               'pathname': '/some/path.py',
               'source': 'eric-desktop'}

    message_params = {k: v for k, v in message.items()
                      # Intentionally ignoring Pylint's reservations about
                      # accessing private members here
                      # pylint: disable=W0212
                      if k in UniqueMessage._fields}
                      # pylint: enable=W0212
    # Make sure we clip after the newline.
    message_params['message'] = '1'
    expected_message = _package_unique_message(message_params)

    # Set up mocks for the queue and buffer.
    message_queue = Mock()
    message_queue.get.return_value = message
    message_buffer = Mock()

    _process_one_message(message_queue, message_buffer)

    # Make sure we sent this message to the buffer as expected.
    message_buffer.add.assert_called_once_with(expected_message,
                                               message['source'])


def test_process_one_fill_exc():
    """
    Test we gracefully handle a message where the message file is split
    over multiple lines. This means the stack trace is also the message. In
    this case we only got message and not exc_text, make sure we fill exc
    text from message.
    We want to clip it so the message is only the first line of the
    message.
    """
    # Build our expected message
    message = {'module': 'log',
               'funcName': 'log_exception',
               'message': '1\n2\n3\n',
               'filename': 'log.py',
               'lineno': 214, 'args': [],
               'kind': 'app',
               'pathname': '/some/path.py',
               'source': 'eric-desktop'}

    message_params = {k: v for k, v in message.items()
                      # Intentionally ignoring Pylint's reservations about
                      # accessing private members here
                      # pylint: disable=W0212
                      if k in UniqueMessage._fields}
                      # pylint: enable=W0212
    # Make sure we clip after the newline and fill exc_text
    message_params['message'] = '1'
    message_params['exc_text'] = '1\n2\n3\n'
    expected_message = _package_unique_message(message_params)

    # Set up mocks for the queue and buffer.
    message_queue = Mock()
    message_queue.get.return_value = message
    message_buffer = Mock()

    _process_one_message(message_queue, message_buffer)

    # Make sure we sent this message to the buffer as expected.
    message_buffer.add.assert_called_once_with(expected_message,
                                               message['source'])


@patch('failnozzle.server.send_email')
def test_mailer_no_recips(send_email_mock):
    """
    Make sure if we don't have anyone to send an email to we send it to a
    fall back address to try to communicate things are broken.
    """
    subject = 'subject'
    report = 'report'
    mailer([], subject, report)
    send_email_mock.assert_called_once_with(setting('REPORT_FROM', ''),
                                            setting('REPORT_TO', ''),
                                            subject,
                                            report,
                                            reply_to=setting('REPLY_TO', ''))


@patch('gevent.joinall')
@patch('gevent.spawn')
def test_flusher_none(spawn, joinall):
    """
    Test that we don't do anything if there is nothing to do.
    """
    template_dir = os.path.join(os.path.dirname(__file__), '..')

    pager_window = 5
    pager_limit = 10

    message_rate = MessageRate(pager_window, pager_limit)

    env = Environment(loader=FileSystemLoader(template_dir))
    message_buffer = MessageBuffer(env.get_template('subject-template.txt'),
                                   env.get_template('body-template.txt'))
    flusher(message_buffer, message_rate)

    # We shouldn't have done anything
    eq_(0, spawn.call_count)
    eq_(0, joinall.call_count)


@patch.multiple('gevent', spawn=DEFAULT, joinall=DEFAULT)
def test_flusher_message(spawn, joinall):
    """
    Test that we email a simple record appropriately.
    """
    template_dir = os.path.join(os.path.dirname(__file__), '..')

    pager_window = 5
    pager_limit = 10

    message_rate = MessageRate(pager_window, pager_limit)

    spawn_ret_val = object()
    spawn.return_value = spawn_ret_val

    env = Environment(loader=FileSystemLoader(template_dir))
    message_buffer = MessageBuffer(env.get_template('subject-template.txt'),
                                   env.get_template('body-template.txt'))

    message = UniqueMessage('module', 'funcName', 'filename', 'message',
                            'pathname', 'lineno', 'exc_text', 'kind')

    message_buffer.add(message, 'source')

    flusher(message_buffer, message_rate)
    spawn.assert_called_once_with(mailer, ANY, ANY, ANY)

    # We shouldn't have done anything
    eq_(1, spawn.call_count)
    eq_(1, joinall.call_count)
    joinall.assert_called_once_with([spawn_ret_val])


@patch.multiple('gevent', spawn=DEFAULT, joinall=DEFAULT)
@patch.multiple('failnozzle.server', pager=DEFAULT, mailer=DEFAULT)
def test_flusher_rate_excede(spawn, joinall, pager, mailer):
    """
    Test that we complain extra loudly if we get too many errors.
    """
    template_dir = os.path.join(os.path.dirname(__file__), '..')

    pager_window = 5
    pager_limit = 10

    message_rate = MessageRate(pager_window, pager_limit)

    spawn_ret_val = object()
    spawn.return_value = spawn_ret_val

    env = Environment(loader=FileSystemLoader(template_dir))
    message_buffer = MessageBuffer(env.get_template('subject-template.txt'),
                                   env.get_template('body-template.txt'))

    for i in range(pager_limit + 1):
        message_buffer.add(UniqueMessage('module', 'funcName', 'filename',
                                         'message', 'pathname', i,
                                         'exc_text', 'kind'),
                           'source')

    flusher(message_buffer, message_rate)
    eq_([call(pager, pager_limit + 1),
         call(mailer, ANY, ANY, ANY)],
        spawn.call_args_list)

    # We shouldn't have done anything
    eq_(2, spawn.call_count)
    eq_(1, joinall.call_count)
    joinall.assert_called_once_with([spawn_ret_val] * 2)
