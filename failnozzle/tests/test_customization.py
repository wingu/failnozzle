"""
Tests for the customization options of the failnozzzle server
"""
from collections import namedtuple
from mock import patch, Mock
from nose.tools import assert_raises, ok_
import os
import sys

from failnozzle.server import _create_queue_rate_buffer, _make_fake_record, \
    _process_one_message, _validate_settings


# Fix path to import failnozzle
TEST_DIR = os.path.dirname(__file__)
sys.path.append(TEST_DIR + '/../..')


# Make our custom unique error which has no fields in common with the default.
# Allows us to make sure we can handle cases where the users idea of an
# exception is completely different than ours.
#
# namedtuples are class-like in usage, so ignore Pylint's objection
# pylint: disable=C0103
CustomUniqueError = namedtuple('CustomUniqueError', ['x', 'y', 'z'])
# pylint: enable=C0103


# Custom values to patch into settings, used by test_customization_tmpl.
CUSTOM_SUBJECT_TEMP = 'custom-sub-template.txt'
CUSTOM_BODY_TEMP = 'custom-sub-template.txt'
CUSTOM_TEMP_DIR = '/custom/templates'


@patch.multiple('failnozzle.settings',
                UNIQUE_MSG_TUPLE=CustomUniqueError,
                SOURCE_FIELD_NAME='src',
                create=True)
def test_customization_msg_impl():
    """
    Test that the user can customize the impl of UniqueMessage and the
    output formats.
    """
    message_queue = Mock()
    message_buffer = Mock()
    message_params = dict(x=1, y=2, z=3, src='src')

    message_queue.get.return_value = message_params
    _process_one_message(message_queue, message_buffer)
    message_buffer.add.assert_called_once_with(CustomUniqueError(1, 2, 3),
                                               'src')


# Have to patch failnozzle.server rather than jinja2 since these are copied
# into the namespace using from/import.
# jinja2.loaders.FileSystemLoader
@patch('failnozzle.server.FileSystemLoader')
# jinja2.environement.Environment
@patch('failnozzle.server.Environment')
@patch.multiple('failnozzle.settings',
                EMAIL_TEMPLATE_DIR=CUSTOM_TEMP_DIR,
                EMAIL_SUBJECT_TEMPLATE=CUSTOM_SUBJECT_TEMP,
                EMAIL_BODY_TEMPLATE=CUSTOM_BODY_TEMP,
                create=True)
def test_customization_tmpl(env_mock, fs_loader_mock):
    """
    Ensure that we can customize the templates used for send mail.
    """
    # Get the Environment instance mock.
    env_inst = env_mock.return_value

    _, _, _ = _create_queue_rate_buffer()

    # Make sure we loaded the expected templates from the expected place.
    fs_loader_mock.assert_called_once_with(CUSTOM_TEMP_DIR)
    env_inst.get_template.assert_called_with(CUSTOM_SUBJECT_TEMP)
    env_inst.get_template.assert_called_with(CUSTOM_BODY_TEMP)


def test_customization_fake_record():
    """
    Test we allow a custom function to make a fake record.
    """
    with patch('failnozzle.settings.INTERNAL_ERROR_FUNC', create=True) as func:
        exc = Exception()
        rec = _make_fake_record(1, exc)

        # At least make sure we get a truthy record back
        ok_(rec)
        ok_(rec.keys())

        # Make sure we passed our values through as expected.
        func.assert_called_once_with(1, exc)


@patch.multiple('failnozzle.settings',
                UNIQUE_MSG_TUPLE=None,
                create=True)
def test_validate_settings():
    """
    Test we blow up if someone customizes us poorly.
    """
    assert_raises(Exception, _validate_settings)
