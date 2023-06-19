"""Extension class file."""
from urllib.parse import urljoin
import base64, json

from fdk_client.platform.PlatformClient import PlatformClient
from fdk_client.platform.PlatformConfig import PlatformConfig
from fdk_client.application.ApplicationClient import ApplicationClient
from fdk_client.common.utils import get_headers_with_signature
from fdk_client.common.aiohttp_helper import AiohttpHelper

from . import __version__
from .constants import ONLINE_ACCESS_MODE, OFFLINE_ACCESS_MODE, FYND_CLUSTER
from .exceptions import FdkInvalidConfig
from .session.session import Session
from .utilities.logger import get_logger, safe_stringify
from .utilities.utility import is_valid_url, get_current_timestamp
from .webhook import WebhookRegistry
from .storage.redis_storage import RedisStorage

from sanic.blueprint_group import BlueprintGroup

logger = get_logger()


class Extension:
    def __init__(self):
        self.api_key: str = None
        self.api_secret: str = None
        self.storage: RedisStorage = None
        self.base_url: str = None
        self.callbacks: dict = None
        self.access_mode: str = None
        self.scopes: list = None
        self.cluster: str = FYND_CLUSTER
        self.webhook_registry: WebhookRegistry = None
        self.__is_initialized: bool = False

    async def initialize(self, data: dict) -> None:
        self.__is_initialized = False

        self.storage = data["storage"]

        # API Key
        if not data.get("api_key"):
            raise FdkInvalidConfig("Invalid api_key")
        self.api_key = data["api_key"]

        # API Secret
        if not data.get("api_secret"):
            raise FdkInvalidConfig("Invalid api_secret")
        self.api_secret = data["api_secret"]

        # Callbacks
        if (not data.get("callbacks") or (data.get("callbacks") and (not data["callbacks"].get("auth") or not data["callbacks"].get("uninstall")))):
            raise FdkInvalidConfig("Missing some of callbacks. Please add all `auth` and `uninstall` callbacks.")
        self.callbacks = data["callbacks"]

        # Access Mode
        self.access_mode = data.get("access_mode") or OFFLINE_ACCESS_MODE

        # Cluster
        if data.get("cluster"):
            if not is_valid_url(data["cluster"]):
                raise FdkInvalidConfig("Invalid cluster")
            self.cluster = data["cluster"]

        # Webhook Registry
        self.webhook_registry = WebhookRegistry()

        # Fetching Extesnion data
        extension_data = await self.get_extension_details()

        # base url
        if (data.get("base_url") and not is_valid_url(data.get("base_url"))):
            raise FdkInvalidConfig(f"Invalid base_url value. Invalid value: {data.get('base_url')}")
        elif (not data.get("base_url")):
            data["base_url"] = extension_data.get("base_url")
        self.base_url = data["base_url"]

        # scopes
        if (data.get("scopes")):
            data["scopes"] = self.verify_scopes(data["scopes"], extension_data)
        self.scopes = data.get("scopes", []) or extension_data["scope"]

        logger.debug("Extension initialized")

        if data.get("webhook_config"):
            await self.webhook_registry.initialize(data["webhook_config"], data)

        self.__is_initialized = True


    def is_initialized(self) -> bool:
        return self.__is_initialized


    def verify_scopes(self, scopes: list, extension_data: dict) -> list:
        missing_scopes = [scope for scope in scopes if scope not in extension_data["scope"]]
        if (not scopes or len(scopes) <= 0 or len(missing_scopes)):
            raise FdkInvalidConfig(f"Invalid scopes in extension config. Invalid scopes: {', '.join(missing_scopes)}")
        return scopes

    def get_auth_callback(self) -> str:
        return urljoin(self.base_url, "/fp/auth")

    def is_online_access_mode(self) -> bool:
        return self.access_mode == ONLINE_ACCESS_MODE

    def get_platform_config(self, company_id) -> PlatformConfig:
        if (not self.is_initialized()):
            raise FdkInvalidConfig("Extension not initialized due to invalid data")

        platform_config = PlatformConfig({
            "companyId": int(company_id),
            "domain": self.cluster,
            "apiKey": self.api_key,
            "apiSecret": self.api_secret,
            "useAutoRenewTimer": False
        })
        return platform_config


    async def get_platform_client(self, company_id, session: Session) -> PlatformClient:
        if (not self.is_initialized()):
            raise FdkInvalidConfig("Extension not initialized due to invalid data")

        from .session.session_storage import SessionStorage

        platform_config = self.get_platform_config(company_id)
        platform_config.oauthClient.setTokenFromSession(session)
        platform_config.oauthClient.token_expires_at = session.access_token_validity

        if (session.access_token_validity and session.refresh_token):
            ac_nr_expired = (session.access_token_validity - get_current_timestamp() // 1000) <= 120
            if ac_nr_expired:
                logger.debug(f"Renewing access token for company {company_id} with platform config {json.dumps(safe_stringify(platform_config))}")
                renew_token_res = await platform_config.oauthClient.renewAccessToken(session.access_mode == OFFLINE_ACCESS_MODE)
                renew_token_res["access_token_validity"] = platform_config.oauthClient.token_expires_at
                session.update_token(renew_token_res)
                await SessionStorage.save_session(session)
                logger.debug(f"Access token renewed for comapny {company_id} with response {renew_token_res}")

        platform_client = PlatformClient(platform_config)
        await platform_client.setExtraHeaders({
            'x-ext-lib-version': f"py/{__version__}"
        })
        return platform_client


    # Making API request to fetch extension details
    async def get_extension_details(self) -> dict:
        try:
            url = f"{self.cluster}/service/panel/partners/v1.0/extensions/details/{self.api_key}"
            token = base64.b64encode(f"{self.api_key}:{self.api_secret}".encode()).decode()
            headers = {
                "Authorization": f"Basic {token}",
                "Content-Type": "application/json",
                'x-ext-lib-version': f"py/{__version__}"
            }
            headers = get_headers_with_signature(
                domain=self.cluster,
                method="get",
                url=f"/service/panel/partners/v1.0/extensions/details/{self.api_key}",
                query_string="",
                headers=headers,
                exclude_headers=list(headers.keys())
            )
            response = await AiohttpHelper().aiohttp_request(request_type="GET", url=url, headers=headers)
            if response["status_code"] == 200:
                return response["json"]
            else:
                raise FdkInvalidConfig(f"{response['json']['message']}, Status: {response['status_code']}")
        except Exception as e:
            raise FdkInvalidConfig(f"Invalid api_key or api_secret. Reason: {str(e)}")


class FdkExtensionClient:

    def __init__(self, **client_data):
        from .api_blueprints import ClientBlueprintGroup

        self.fdk_route: BlueprintGroup = client_data["fdk_handler"]
        self.extension: Extension = client_data["extension"]
        self.platform_api_routes: ClientBlueprintGroup = client_data["platform_api_routes"]
        self.webhook_registry: WebhookRegistry = client_data["webhook_registry"]
        self.application_proxy_routes: ClientBlueprintGroup = client_data["application_proxy_routes"]
        self.get_platform_client: PlatformClient = client_data["get_platform_client"]
        self.get_application_client: ApplicationClient = client_data["get_application_client"]


extension = Extension()