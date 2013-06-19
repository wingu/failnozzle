"""
Settings for failnozzle.
"""
import imp
import logging
import os
import re


###############################################################################
# Exception Email Settings                                                    #
###############################################################################
# Address to send exception emails to
# REPORT_TO = 'your.team@yourcompany.com'
# Address to send exception emails from
# REPORT_FROM = 'error.reporter@yourcompany.com'
# Optional Reply-To address:
# REPLY_TO = 'error.reporter@yourcompany.com'
# How often to send aggregated exception emails.
FLUSH_SECONDS = 60


###############################################################################
# SMTP Server Settings                                                        #
###############################################################################
# SMTP_HOST = 'smtp.yourcompany.com'
# SMTP_PORT = 465
# SMTP_USER = 'error.reporter@yourcompany.com'
# SMTP_PASSWORD = 'drink canada pony solvent'


###############################################################################
# Page settings                                                               #
###############################################################################
# If our exception count in a given window is higher than what we allow we'll #
# send an additional email to the configured address.                         #
# Recommended to set this to a PagerDuty or other alerting mechanism.         #
###############################################################################
# PAGER_TO = 'your.pager.address@yourcompany.com'
# PAGER_FROM = 'error.reporter@yourcompany.com'
# Optional Reply-To address:
# PAGER_REPLY_TO = 'error.reporter@yourcompany.com'

# If we receive PAGER_WINDOW_LIMIT exception in
# PAGER_WINDOW_SIZE * FLUSH_SECONDS seconds, send a page to the configured
# address.
PAGER_WINDOW_SIZE = 5
PAGER_LIMIT = 100


###############################################################################
# Monitoring Configuration                                                    #
###############################################################################
# If configured, sends errors with specific strings in them to a different    #
# email address. This is useful to monitor that systems are correctly sending #
# messages to failnozzle.                                                     #
###############################################################################
# JUST_MONITORING_REPORT_TO = 'error.canary@yourcompany.com'

# Unambiguous strings to look for in exceptions which signify that the current
# exception is not a real exception. Used to monitor that this service is
# working, should not be used to control whether or not an email is sent.
MONITORING_ERROR_MARKERS = ["1669fe88-b0c3-439d-bc10-8a3d21493ede",
                            "c84a3673-0a95-447b-810f-8107e1e38013"]


###############################################################################
# Customization Config                                                        #
###############################################################################
# Allows customization of what constitutes a unique message and how           #
# aggregated exception emails appear.                                         #
###############################################################################
# Override the definition of a unique message by setting a namedtuple whose
# fields define a unique message.
# UNIQUE_MSG_TUPLE = <custom_val>

# Defines which field of the incoming message holds the source of a message.
SOURCE_FIELD_NAME = 'source'

# Set to a directory where templates will be found.
# EMAIL_TEMPLATE_DIR = <custom_dir>

# Defines Jinja2 templates to use for rendering of an exception email
# body/subject
EMAIL_BODY_TEMPLATE = 'body-template.txt'
EMAIL_SUBJECT_TEMPLATE = 'subject-template.txt'

# When an internal error occurs we will attempt to communicate using
# failnozzle. This function will take the parameters count and exception
# and generate a dict that will eventually be converted to the class set
# to UNIQUE_MSG_IMPL.
# INTERNAL_ERROR_FUNC = <internal error func>

###############################################################################
# Internal Config                                                             #
###############################################################################
# The name of this server
if hasattr(os, 'uname'):
    SERVER_NAME = os.uname()[1]
else:
    # In case of Windows
    import socket
    SERVER_NAME = socket.gethostname()

# Log configuration of this service.
LOG_LEVEL = logging.DEBUG
LOG_FORMAT = '%(asctime)-15s %(levelname)-8s %(message)s'

INCOMING_MESSAGE_MAX_SIZE = 65536

# Address/port to listen for messages on.
UDP_BIND = ('0.0.0.0', 1549)

# Load override files so users only have to specify the necessary parameters.
base_dir = os.path.dirname(__file__)

# Deploy settings
deploy_file = os.path.join(base_dir, 'deploy_settings.py')
if os.path.exists(deploy_file):
    execfile(deploy_file)

# Local dev settings, shouldn't be checked in.
local_file = os.path.join(base_dir, 'local_settings.py')
if os.path.exists(local_file):
    execfile(local_file)


def import_config_file(config_file):
    """
    Import a config file given to us through an argument, possibly overriding
    any of our initial settings
    """
    if os.path.exists(config_file):
        setting_re = re.compile("^[_A-Z]+$")
        module = imp.load_source('failnozzle.config_file', config_file)

        # Add all the GLOBAL_SETTINGS in the config_file to this module
        overwrite_count = 0
        for name in dir(module):
            if setting_re.match(name):
                globals()[name] = getattr(module, name)
                overwrite_count += 1

        print "Overwrote %s settings" % overwrite_count
    else:
        print "Config file %s not found" % config_file
        exit(1)
