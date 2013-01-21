"""
A wrapper for DatagramHandler that adds information about the source of the log
message, and the service that generated it.  Also, encodes the messages as JSON
instead of python's pickle format.
"""
import json
import logging.handlers
import struct


class AggregatorHandler(logging.handlers.DatagramHandler):
    """
    Wraps DatagramHandler with some additional information about the source
    and kind of each log message.
    """

    def __init__(self, host, port, source, kind):
        """
        `host`: the host of the log aggregator service
        `port`: the port number of the log aggregator service
        `source`: the hostname of the machine that generated the log message
        `kind`: the kind of service that generated the message (app, imap, ...)
        """
        self.source = source
        self.kind = kind
        super(AggregatorHandler, self).__init__(host, port)

    def makePickle(self, record):
        """
        Marshalls the record, in this case to JSON rather than a pickle string
        and converts the record to binary format with a length prefix, and
        returns it ready for transmission across the socket.

        See logging.handlersSocketHandler.makePickle, we follow the same
        conventions and logic only w/ JSON rather than pickle.
        """
        ei = record.exc_info
        if ei:
            # just to get traceback text into record.exc_text
            dummy = self.format(record)
            # to avoid json error
            record.exc_info = None
        s = json.dumps(record.__dict__)
        # for next handler
        if ei:
            record.exc_info = ei
        return s

    def emit(self, record):
        """
        Emits the record, adding `source` and `kind` fields.
        """
        record.source = self.source
        record.kind = self.kind
        return super(AggregatorHandler, self).emit(record)
