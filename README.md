# failnozzle

`failnozzle` is a standalone daemon that receives log messages as JSON objects over
UDP, and batches them into email digests. We created it at Wingu to prevent
floods of web application error emails from creating a long backlog in our
outgoing mail server.

Rather than having your application nodes talk to an SMTP server directly, they
send JSON to the `failnozzle` daemon, which accumulates the messages into summary
emails and periodically sends them to an SMTP server.

    ----------
    | Node 1 | ------
    ----------      |
                    |
    ----------      |   JSON     --------------             --------
    | Node 2 | ----------------> | failnozzle | ----------> | SMTP |
    ----------      |            --------------             --------
                    |
    ----------      |
    | Node 3 | ------
    ----------

`failnozzle` is implemented in Python and uses [gevent][gevent] for networking and
concurrency.


## Installation

Since gevent requires libevent and its headers, you should install these
via your package manager. On Debian or Ubuntu, these are found in the
`libevent-dev` package.

If you have a fairly recent version of `pip`, install `failnozzle` system-wide using:

    pip install git+https://github.com/wingu/failnozzle

or locally, to a [virtualenv][virtualenv] named `failnozzle`:

    pip install -E failnozzle git+https://github.com/wingu/failnozzle

Otherwise, install gevent and Jinja2, then clone the git repository and
install from `setup.py`:

    git clone https://github.com/wingu/failnozzle
    cd failnozzle; python setup.py install


## Dependencies

* Python 2.7 (we have not yet tested `failnozzle` on other Python versions -- YMMV)
* [gevent][gevent]
* [Jinja2][jinja]


## Running

To run `failnozzle`:

    python -m failnozzle.server [optional path to config overrides]

By default, `failnozzle` emits logging to stderr.


## Sending messages to `failnozzle` from an application

Because `failnozzle` receives JSON-formatted messages on a UDP port, you can
communicate with it from any programming language or framework. We have
included our Python implementation, which is implemented as a handler for the
standard Python [`logging`][logging] module.

To use it from a `logging` file-based configuration,

    [logger_myapp]
    level=INFO
    handlers=failnozzleHandler
    qualname=myapp

    [handler_failnozzleHandler]
    class=failnozzle.loghandler.AggregatorHandler
    level=ERROR
    args=('failnozzle.example.com', 1549, os.uname()[1], 'myapp')

If you want to use Failnozzle from a non-Python application, you'll
get deduping and digest out of the box by sending json that looks like
this:

```
{
 "module": <your module name>,
 "funcName": <your function name>,
 "filename": <your file name>,
 "pathname": <your path name>,
 "lineno": <your line number>,
 "message": <your error message>,
 "exc_text": <the text of the exception, e.g. stack trace>,
 "kind": <some discriminator, e.g. your app name>
}
```

Alternatively, you can create your own named tuple with the fields you
want to send in your JSON and override UNIQUE_MSG_TUPLE in the
server's settings to that tuple name (see Configuration below for
where to find settings).

## Configuration

`failnozzle` is configured from the `failnozzle.settings` module, and allows you to
specify a config file that overrides the defaults. The config file is simply a
Python module that assigns configuration variables.

Some relevant settings are as follows:

* `UDP_BIND`: a tuple of (hostname string, port number) for the UDP socket to
  bind
* `SMTP_HOST`, `SMTP_PORT`: the hostname and port number of the SMTP server
  `failnozzle` will use to send mail
* `SMTP_USER`, `SMTP_PASSWORD`: if necessary, the username and password for
  authenticating to the SMTP server
* `REPORT_FROM`: the "From" address for summary emails sent by `failnozzle`
* `REPORT_TO`: the destination address for summary emails
* `JUST_MONITORING_REPORT_TO`: the destination address for reports that
  contain only "just monitoring" messages (for end-to-end monitoring)
* `MONITORING_ERROR_MARKERS`: a list of marker strings that signals `failnozzle`
  to treat a message as "just monitoring"
* `PAGER_FROM`: the "From" address for alert emails sent by `failnozzle`
* `PAGER_TO`: the destination address for alert emails
* `FLUSH_SECONDS`: the number of seconds between flushes of `failnozzle`'s buffer
  (error emails will be sent no more frequently than this number of seconds)
* `PAGER_WINDOW_SIZE`, `PAGER_WINDOW_LIMIT`: if more than
  `PAGER_WINDOW_LIMIT` messages are received in `PAGER_WINDOW_SIZE` flushes, an
  alert email will be triggered to `PAGER_TO`


## Summary Email Examples

An example summary email using the default template looks like this:

    ** 5 instances of 2 unique errors (service1, service2) **

    ========
    Summary:
    ========
    4X Exception in view: Traceback (most recent call last): (in service1, src/service1/views.py:50)
    1X Could not retrieve file "foo.txt" (in service2, src/service2/files.py:363)

    ========
    Details:
    ========
    Exception #1 of 2: 4X Exception in view: Traceback (most recent call last): (in service1, src/service1/views.py:50)

    Seen between 2013-01-09 15:08:41.781727 to 2013-01-09 15:08:45.426238
    - on host1, 1X
    - on host2, 3X

    Traceback (most recent call last):
      ...
      File "src/service1/views.py", line 50, in myfunc
         raise SomeException("fail!")
    SomeException: fail!
    ----------------------------------------------------------------------

    Exception #2 of 2: 1X Could not retrieve file "foo.txt" (in service2, src/service2/files.py:363)

    Seen between 2013-01-09 15:08:41.820702 to 2013-01-09 15:08:41.820702
    - on host3, 1X

    Traceback (most recent call last):
      ...
      File "src/service1/views.py", line 50, in myfunc
         with open(filename, 'r') as handle:
    IOError: [Errno 2] No such file or directory: 'foo.txt'

    [EOM]


## Running in Production With Supervisor

In production, we recommend running `failnozzle` using [supervisor][supervisor],
using a configuration like the following:

    [program:failnozzle]
    command=[path to virtualenv]/bin/python -m failnozzle.server [path to config]
    process_name=failnozzle
    user=[some non-root user]
    autorestart=true
    stdout_logfile=/var/log/failnozzle.stdout.log
    stderr_logfile=/var/log/failnozzle.stderr.log

Then, once the Supervisor daemon is running, you can start and stop `failnozzle` as
a daemon using `supervisorctl`.


## Design

`failnozzle` uses a pipeline of greenlets (green threads) connected by queues to
process incoming data.

The main greenlet listens for incoming UDP packets that contain a JSON-encoded
message. When a packet is received, its contents are decoded and queued for the
processing greenlet.

The processing greenlet builds a unique key for each message it pops from the
queue, and stores it in a buffer. If the message's unique key does not yet
exist in the buffer, it is added as a new message. If it does exist, it is
added as a new instance of the existing message. The unique key is
customizable, and is typically extracted from the contents of the message (but
not its source or timestamp).

The timer greenlet periocially flushes the message buffer, creating and sending
a single email message that summarizes the unique messages with some details
about their instances (e.g., the number of times the message was received from
each host). The buffer also tracks the rate of incoming messages, so that the
timer greenlet can create and send an alert email (to a pager, for instance) if
the rate exceeds some threshhold.

In addition, `failnozzle` can optionally ignore certain kinds of automated messages
based on the presence of a marker string.  For example, we perodically trigger
a special "just for monitoring" error in our app, to perform a regular
end-to-end test of our live error reporting pathway. `failnozzle` does not send a
summary email if it would consist entirely of these automated messages.


## Development

To run the unit tests, you'll need the nose and mock packages.  Once those are
installed, you can run the tests via:

    nosetests failnozzle


[gevent]: http://www.gevent.org
[jinja]: http://jinja.pocoo.org/
[virtualenv]: http://www.virtualenv.org
[supervisor]: http://pypi.python.org/pypi/supervisor
[logging]: http://docs.python.org/2/library/logging.html
