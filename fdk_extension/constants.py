# cluster constants
FYND_CLUSTER = "https://api.fynd.com"

# access modes
OFFLINE_ACCESS_MODE = "offline"
ONLINE_ACCESS_MODE = "online"

SESSION_COOKIE_NAME = "ext_session"
ADMIN_SESSION_COOKIE_NAME='ext_adm_session'

SESSION_EXPIRY_IN_SECONDS = 900

ASSOCIATION_CRITERIA = {
    "ALL": "ALL",
    "SPECIFIC": "SPECIFIC-EVENTS",  # to be set when saleschannel specific events are subscribed & sales channel present
    "EMPTY": "EMPTY"  # to be set when saleschannel specific events are subscribed but not sales channel present
}

TEST_WEBHOOK_EVENT_NAME = "ping"