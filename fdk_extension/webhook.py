"""Webhook utility."""
import hashlib
import hmac
import re

import ujson


from .constants import ASSOCIATION_CRITERIA, TEST_WEBHOOK_EVENT_NAME
from .exceptions import FdkInvalidHMacError
from .exceptions import FdkInvalidWebhookConfig
from .exceptions import FdkWebhookHandlerNotFound
from .exceptions import FdkWebhookProcessError
from .exceptions import FdkWebhookRegistrationError
from .utilities.logger import get_logger
from .utilities.aiohttp_retry import retry_middleware

from fdk_client.common.aiohttp_helper import AiohttpHelper
from fdk_client.common.utils import get_headers_with_signature
from fdk_client.platform.PlatformClient import PlatformClient

from sanic.request import Request


logger = get_logger()

event_config = {}

class WebhookRegistry:
    def __init__(self):
        self._handler_map = None
        self._config : dict = None
        self._fdk_config : dict = None

    async def initialize(self, config: dict, fdk_config: dict):
        email_regex_match = r"^\S+@\S+\.\S+$"
        if not config.get("notification_email") or not re.findall(email_regex_match, config["notification_email"]):
            raise FdkInvalidWebhookConfig("Invalid or missing notification_email")

        if not config.get("api_path") or config["api_path"][0] != "/":
            raise FdkInvalidWebhookConfig("Invalid or missing api_path")

        if not config.get("event_map"):
            raise FdkInvalidWebhookConfig("Invalid or missing event_map")

        config["subscribe_on_install"] = config.get("subscribe_on_install", True)
        self._handler_map = {}
        self._config = config
        self._fdk_config = fdk_config

        handler_config = {}

        for (event_name, handler_data) in self._config["event_map"].items():
            handler_config[event_name] = handler_data

        await self.get_event_config(handler_config=handler_config)
        event_config["events_map"] = self.__get_event_id_map(event_config.get("event_configs"))
        self.__validate_events_map(handler_config)

        if len(event_config["event_not_found"]):
            errors = []
            for key in event_config["event_not_found"]:
                errors.append(ujson.dumps({"name": key, "version": event_config["event_not_found"]["key"]}))

            raise FdkInvalidWebhookConfig(f"Webhooks events {', '.join(errors)} not found")

        self._handler_map = handler_config
        logger.debug('Webhook registry initialized')

    @property
    def is_initialized(self) -> bool:
        return self._handler_map and self._config["subscribe_on_install"]


    def __validate_events_map(self, handler_config: dict):
        event_config.pop("event_not_found", None)
        event_config["event_not_found"] = {}

        for key in handler_config.keys():
            if not f"{key}/{handler_config[key]['version']}" in event_config["events_map"]:
                event_config["event_not_found"][key] = handler_config[key]["version"]


    def __get_event_id_map(self, events: list) -> dict:
        event_map = {}
        for event in events:
            event_map[f"{event['event_category']}/{event['event_name']}/{event['event_type']}/{event['version']}"] = event['id']
        return event_map


    def __association_criteria(self, application_id_list: list) -> str:
        if self._config.get("subscribed_saleschannel") == "specific":
            return ASSOCIATION_CRITERIA["SPECIFIC"] if application_id_list else ASSOCIATION_CRITERIA["EMPTY"]
        return ASSOCIATION_CRITERIA["ALL"]

    @property
    def __webhook_url(self) -> str:
        return f"{self._fdk_config['base_url']}{self._config['api_path']}"

    def __is_config_updated(self, subscriber_config: dict) -> bool:
        updated = False
        config_criteria = self.__association_criteria(subscriber_config["association"]["application_id"])
        if config_criteria != subscriber_config["association"].get("criteria"):
            if config_criteria == ASSOCIATION_CRITERIA["ALL"]:
                subscriber_config["association"]["application_id"] = []
            logger.debug(f"Webhook association criteria updated from {subscriber_config['association'].get('criteria')}"
                         f"to {config_criteria}")
            subscriber_config["association"]["criteria"] = config_criteria
            updated = True

        if self._config["notification_email"] != subscriber_config["email_id"]:
            logger.debug(f"Webhook notification email updated from {subscriber_config['email_id']} "
                         f"to {self._config['notification_email']}")
            subscriber_config["email_id"] = self._config["notification_email"]
            updated = True

        if self.__webhook_url != subscriber_config["webhook_url"]:
            logger.debug(f"Webhook url updated from {subscriber_config['webhook_url']} to {self.__webhook_url}")
            subscriber_config["webhook_url"] = self.__webhook_url
            updated = True

        return updated

    async def sync_events(self, platform_client: PlatformClient, config: dict=None, enable_webhooks: bool=None):
        if not self.is_initialized:
            raise FdkInvalidWebhookConfig("Webhook registry not initialized")
        logger.debug("Webhook sync events started")
        if config:
            await self.initialize(config, self._fdk_config)

        subscriber_config: dict = await self.get_subscribe_config(platform_client=platform_client)
        register_new = False
        config_updated = False
        existing_events = []

        if not subscriber_config:
            subscriber_config = {
                "name": self._fdk_config["api_key"],
                "webhook_url": self.__webhook_url,
                "association": {
                    "company_id": platform_client.config.companyId,
                    "application_id": [],
                    "criteria": self.__association_criteria([])
                },
                "status": "active",
                "auth_meta": {
                    "type": "hmac",
                    "secret": self._fdk_config["api_secret"]
                },
                "event_id": [],
                "email_id": self._config["notification_email"]
            }
            register_new = True
            if enable_webhooks is not None:
                subscriber_config["status"] = "active" if enable_webhooks else "inactive"
        else:
            logger.debug(f"Webhook config on platform side for company id {platform_client.config.companyId}: {ujson.dumps(subscriber_config)}")

            auth_meta = subscriber_config["auth_meta"]
            event_configs = subscriber_config["event_configs"]
            for key in list(subscriber_config.keys()): 
                if key not in ["id", "name", "webhook_url", "association", "status", "auth_meta", "email_id"]:
                    subscriber_config.pop(key)

            subscriber_config["event_id"] = []
            existing_events = [each_event["id"] for each_event in event_configs]

            if auth_meta["secret"] != self._fdk_config["api_secret"]:
                auth_meta["secret"] = self._fdk_config["api_secret"]
                config_updated = True

            if enable_webhooks is not None:
                subscriber_config["status"] = "active" if enable_webhooks else "inactive"
                config_updated = True
            
            if self.__is_config_updated(subscriber_config):
                config_updated = True

        for event_name in self._handler_map.keys():
            event_name = f"{event_name}/{self._handler_map[event_name]['version']}"
            event_id = event_config["events_map"][event_name]
            if event_id:
                subscriber_config["event_id"].append(event_id)


        try:
            if register_new:
                await platform_client.webhook.registerSubscriberToEvent(body=subscriber_config)

                if self._fdk_config.get("debug", None):
                    event_map = {}
                    for event_name in event_config["events_map"]:
                        event_map[event_config["events_map"][event_name]] = event_name
                    subscriber_config["event_id"] = [event_map[event_id] for event_id in subscriber_config["event_id"]]
                    logger.debug(f"Webhook config registered for company: {platform_client.config.companyId}, config: {ujson.dumps(subscriber_config)}")
                
            else:
                event_diff = [each_event_id for each_event_id in subscriber_config["event_id"]
                              if each_event_id not in existing_events]
                event_diff.extend([each_event_id for each_event_id in existing_events
                                   if each_event_id not in subscriber_config["event_id"]])

                if event_diff or config_updated:
                    await platform_client.webhook.updateSubscriberConfig(body=subscriber_config)

                    if self._fdk_config.get("debug", None):
                        event_map = {}
                        for event_name in event_config["events_map"]:
                            event_map[event_config["events_map"][event_name]] = event_name
                        subscriber_config["event_id"] = [event_map[event_id] for event_id in subscriber_config["event_id"]]
                        logger.debug(f"Webhook config updated for company: {platform_client.config.companyId}, config: {ujson.dumps(subscriber_config)}")

        except Exception as e:
            raise FdkWebhookRegistrationError(f"Failed to sync webhook events. Reason: {str(e)}")


    async def enable_sales_channel_webhook(self, platform_client: PlatformClient, application_id: str):
        if not self.is_initialized:
            raise FdkInvalidWebhookConfig("Webhook registry not initialized")

        if self._config.get("subscribed_saleschannel") != "specific":
            raise FdkWebhookRegistrationError("'subscribed_saleschannel' is not set to 'specific' in webhook config")
        
        try:
            subscriber_config = await self.get_subscribe_config(platform_client=platform_client)
            
            if not subscriber_config:
                raise FdkWebhookRegistrationError("Subscriber config not found")

            event_configs = subscriber_config["event_configs"]
            for key in list(subscriber_config.keys()):
                if key not in ["id", "name", "webhook_url", "association", "status", "auth_meta", "email_id"]:
                    subscriber_config.pop(key)

            subscriber_config["event_id"] = [each_event["id"] for each_event in event_configs]
            arr_application_id = subscriber_config["association"].get("application_id") or []
            try:
                arr_application_id.index(application_id)
            except ValueError:
                arr_application_id.append(application_id)
                subscriber_config["association"]["application_id"] = arr_application_id
                subscriber_config["association"]["criteria"] = self.__association_criteria(subscriber_config["association"]["application_id"])
                await platform_client.webhook.updateSubscriberConfig(body=subscriber_config)
                logger.debug(f"Webhook enabled for saleschannel: {application_id}")

        except Exception as e:
            raise FdkWebhookRegistrationError(f"Failed to add saleschannel webhook. Reason: {str(e)}")


    async def disable_sales_channel_webhook(self, platform_client: PlatformClient, application_id: str):
        if not self.is_initialized:
            raise FdkInvalidWebhookConfig("Webhook registry not initialized")
        
        if self._config.get("subscribed_saleschannel") != "specific":
            raise FdkWebhookRegistrationError("`subscribed_saleschannel` is not set to `specific` in webhook config")
        try:
            subscriber_config = await self.get_subscribe_config(platform_client=platform_client)
            if not subscriber_config:
                raise FdkWebhookRegistrationError("Subscriber config not found")

            event_configs = subscriber_config["event_configs"]
            for key in list(subscriber_config.keys()):
                if key not in ["id", "name", "webhook_url", "association", "status", "auth_meta", "email_id"]:
                    subscriber_config.pop(key)

            subscriber_config["event_id"] = [each_event["id"] for each_event in event_configs]
            arr_application_id = subscriber_config["association"].get("application_id") or []
            if application_id in arr_application_id:
                arr_application_id.remove(application_id)
                subscriber_config["association"]["criteria"] = self.__association_criteria(subscriber_config["association"].get("application_id", []))
                subscriber_config["association"]["application_id"] = arr_application_id
                await platform_client.webhook.updateSubscriberConfig(body=subscriber_config)
                logger.debug(f"Webhook disabled for saleschannel: {application_id}")

        except Exception as e:
            raise FdkWebhookRegistrationError(f"Failed to disabled saleschannel webhook. Reason: {str(e)}")

    def verify_signature(self, request: Request):
        req_signature = request.headers['x-fp-signature']
        calculated_signature = hmac.new(self._fdk_config["api_secret"].encode(),
                                        request.body,
                                        hashlib.sha256).hexdigest()
        if req_signature != calculated_signature:
            raise FdkInvalidHMacError("Signature passed does not match calculated body signature")

    async def process_webhook(self, request: Request):
        if not self.is_initialized:
            raise FdkInvalidWebhookConfig("Webhook registry not initialized")
        try:
            body = request.json
            if body["event"]["name"] == TEST_WEBHOOK_EVENT_NAME:
                return
            self.verify_signature(request)
            event_name = f"{body['event']['name']}/{body['event']['type']}"
            category_event_name = event_name
            if body["event"].get("category"):
                category_event_name = f"{body['event']['category']}/{event_name}"

            event_handler_map = self._handler_map.get(category_event_name) or self._handler_map.get(event_name) or {}
            ext_handler = event_handler_map.get("handler")

            if callable(ext_handler):
                logger.debug(f"Webhook event received for company: {body['company_id']}, "
                             f"application: {body.get('application_id', '')}, event name: {event_name} ")
                await ext_handler(event_name, body, body["company_id"], body["application_id"])
            else:
                raise FdkWebhookHandlerNotFound(f"Webhook handler not assigned: {category_event_name}")
        except Exception as e:
            raise FdkWebhookProcessError(str(e))


    async def get_subscribe_config(self, platform_client: PlatformClient) -> dict:
        try:
            subscriber_config = await platform_client.webhook.getSubscribersByExtensionId(extension_id=self._fdk_config["api_key"])
            return subscriber_config["json"]["items"][0] if subscriber_config["json"]["items"] else None
        except Exception as e:
            raise FdkInvalidWebhookConfig(f"Error while fetching webhook subscriber configuration, Reason: {str(e)}")


    async def get_event_config(self, handler_config: dict) -> dict:
        try:
            data = []
            for key in handler_config.keys():
                event_dict = {}
                event_details = key.split("/")
                if len(event_details) != 3:
                    raise FdkInvalidWebhookConfig(f"Invalid webhook event map key. Invalid key: {key}")

                event_dict["event_category"] = event_details[0]
                event_dict["event_name"] = event_details[1]
                event_dict["event_type"] = event_details[2]
                event_dict["version"] = handler_config[key].get("version")
                data.append(event_dict)

            url = f"{self._fdk_config.get('cluster')}/service/common/webhook/v1.0/events/query-event-details"
            headers= {
                "Content-Type": "application/json"
            }
            headers = get_headers_with_signature(
                domain=self._fdk_config.get('cluster'),
                method="post",
                url="/service/common/webhook/v1.0/events/query-event-details",
                query_string="",
                headers=headers,
                body=data,
                exclude_headers=list(headers.keys())
            )
            response = await retry_middleware(AiohttpHelper().aiohttp_request, request_type="POST", url=url, headers=headers, data=data)
            response_data: dict = response["json"]
            event_config["event_configs"] = response_data.get("event_configs")
            logger.debug(f"Webhook events config received: {ujson.dumps(response_data)}")
            return response_data

        except Exception as e:
            raise FdkInvalidWebhookConfig(f"Error while fetching webhook events configuration, Reason: {str(e)}")