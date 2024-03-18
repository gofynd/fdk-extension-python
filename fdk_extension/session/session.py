import hashlib
import json
import uuid

from ..constants import ONLINE_ACCESS_MODE
from ..utilities.utility import isoformat_to_datetime
from ..utilities.utility import json_serial


class Session:
    def __init__(self, session_id: str, is_new=True):
        self.session_id: str = session_id
        self.company_id: int = None
        self.organization_id: int = None
        self.state: str = None
        self.scope: list = None
        self.expires: int = None
        self.expires_in: int = None
        self.access_token_validity: int = None
        self.access_mode: str = ONLINE_ACCESS_MODE
        self.access_token: str = None
        self.current_user: dict = None
        self.refresh_token: str = None
        self.is_new: bool = is_new
        self.extension_id: str = None


    @staticmethod
    def clone_session(session):
        session_object = Session(session["session_id"], session["is_new"])
        for key in session:
            if key in ["expires"]:
                if session[key]:
                    session[key] = isoformat_to_datetime(session[key])
            setattr(session_object, key, session[key])
        return session_object

    def update_token(self, raw_token: dict):
        self.access_mode = raw_token.get("access_mode")
        self.access_token = raw_token.get("access_token")
        self.current_user = raw_token.get("current_user")
        self.refresh_token = raw_token.get("refresh_token")
        self.expires_in = raw_token.get("expires_in")
        self.access_token_validity = raw_token.get("access_token_validity")

    def to_json(self):
        return json.dumps(self.__dict__, default=json_serial)

    @staticmethod
    def generate_session_id(is_online, **config_options):
        if is_online:
            return str(uuid.uuid4())
        else:
            return hashlib.sha256(
                "{}:{}".format(config_options["cluster"], config_options["id"]).encode()).hexdigest()
