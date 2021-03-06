import logging
import voluptuous as vol
import requests
from datetime import datetime, timedelta

import homeassistant.helpers.config_validation as cv
from homeassistant.components.lock import LockDevice, PLATFORM_SCHEMA
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_ID, CONF_USERNAME, CONF_PASSWORD

_LOGGER = logging.getLogger(__name__)

DOMAIN = "doorman"
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ACCESS_TOKEN): cv.string,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_ID): cv.string,
    }
)

BASE_URL = "https://mob.yalehomesystem.co.uk/yapi"
API_LOGIN_URL = BASE_URL + "/o/token/"
API_STATE_URL = BASE_URL + "/api/panel/cycle/"
API_HISTORY_URL = BASE_URL + "/api/event/report/?page_num=1&set_utc=1"
SCAN_INTERVAL = timedelta(seconds=10)

STATE_ENUM = {
    "1816": "device_status.lock",  # Locked after a failed lock
    "1815": "device_status.unlock",  # Failed to lock
    "1807": "device_status.lock",  # Auto-relocked
    "1801": "device_status.unlock",  # Unlock from inside
    "1802": "device_status.unlock",  # Unlock from outside, token or keypad,
}

NON_LOCK_EVENT = {"1602": "device_status.lock"}  # Periodic test


def login(username, password, initial_token):
    _LOGGER.info("Trying to log in to Yale..")
    headers = {"Accept": "application/json", "Authorization": f"Basic {initial_token}"}
    auth_data = {"grant_type": "password", "username": username, "password": password}
    res = requests.post(API_LOGIN_URL, data=auth_data, headers=headers)
    login_data = res.json()
    now = datetime.now()
    timestamp = datetime.timestamp(now)
    login_data["loggedin"] = timestamp
    _LOGGER.info("Logged in to Yale")
    return login_data


async def async_setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Doorman platform."""
    username = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    initial_token = config.get(CONF_ACCESS_TOKEN)
    login_data = login(username, password, initial_token)
    token = login_data["access_token"]
    device_id = config.get(CONF_ID)
    response = requests.get(API_STATE_URL, headers={"Authorization": f"Bearer {token}"}, timeout=5)
    if response.status_code == 200:
        _LOGGER.info("Setting up Doorman platform")
        data = response.json()
        status = data["message"]
        if status == "OK!":
            devices = data.get("data").get("device_status")
            for device in devices:
                device_id = device.get("device_id")
                name = device.get("name")
                state = device.get("status_open")[0]
                _LOGGER.info(f"Adding device {name}, setting status to {state}")
                add_entities(
                    [Doorman(state, login_data, username, password, name, device_id, initial_token)]
                )
        else:
            _LOGGER.info(f"Status is not OK!: {status}")
    else:
        _LOGGER.error("Error retrieving doorman lock status during init: %s", response.text)


class Doorman(LockDevice):
    """Representation of a Yale Doorman lock."""

    LOCK_STATE = "device_status.lock"
    UNLOCK_STATE = "device_status.unlock"
    FAILED_STATE = "failed"

    def __init__(self, state, login_data, username, password, name, device_id, initial_token):
        """Initialize the lock."""
        self._state = state
        self.username = username
        self.password = password
        self.login_data = login_data
        self.device_id = device_id
        self.initial_token = initial_token
        self._name = name
        self.report_ids = []

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def is_locked(self):
        """Return True if the lock is currently locked, else False."""
        return self._state == Doorman.LOCK_STATE

    def lock(self, **kwargs):
        """Lock the device."""
        # self._state = self.do_change_request(Doorman.LOCK_STATE)

    def unlock(self, **kwargs):
        """Unlock the device."""
        # self._state = self.do_change_request(Doorman.UNLOCK_STATE)

    def get_state(self):
        self.validate_access_token()
        access_token = self.login_data["access_token"]
        response = requests.get(
            API_STATE_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=5
        )
        if response.status_code == 200:
            data = response.json()
            status = data["message"]
            if status == "OK!":
                devices = data.get("data").get("device_status")
                for device in devices:
                    device_id = device.get("device_id")
                    name = device.get("name")
                    state = device.get("status_open")[0]
                    if name == self._name:
                        if state != Doorman.LOCK_STATE and state != Doorman.UNLOCK_STATE:
                            _LOGGER.info(f"Setting state to {state}")
                        return state
            else:
                _LOGGER.error(f"Status is not OK!: {status}")
        else:
            _LOGGER.error("Error retrieving doorman lock status during update: %s", response.text)

    def validate_access_token(self):
        """ Verify that our access token is still valid """
        now = datetime.now()
        timestamp = datetime.timestamp(now)
        # Check if logg-in time + expires in is past now(-ish)
        if (self.login_data["loggedin"] + self.login_data["expires_in"]) <= (timestamp - 1000):
            _LOGGER.info("Token expired. Logging in to Yale")
            self.login_data = login(self.username, self.password, self.initial_token)

    def get_state_history(self):
        self.validate_access_token()
        access_token = self.login_data["access_token"]
        data = {"page_num": 1, "set_utc": 1}
        response = requests.get(
            API_HISTORY_URL,
            data=data,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=5,
        )
        if response.status_code == 200:
            data = response.json()
            status = data["message"]
            states = []
            if status == "OK!":
                for event in data.get("data"):
                    device_type = event.get("type")
                    if device_type == "device_type.door_lock":
                        report_id = event.get("report_id")
                        if report_id not in self.report_ids:
                            name = event.get("name")
                            user = event.get("user")
                            event_type = event.get("event_type")
                            self.report_ids.append(report_id)
                            if event_type in NON_LOCK_EVENT:
                                continue
                            state = STATE_ENUM[event_type]
                            _LOGGER.info(f"Parsing event: {report_id} it has {state}")
                            states.append(state)

            else:
                _LOGGER.error(f"Status is not OK!: {status}")
        else:
            _LOGGER.error("Error retrieving doorman lock status during update: %s", response.text)
        return states

    def update(self):
        """Update the internal state of the device."""
        states = self.get_state_history()
        for state in states:
            self._state = state
            self.async_write_ha_state()
        self._state = self.get_state()

    # def do_change_request(self, requested_state):
    #     """Execute the change request and pull out the new state."""
