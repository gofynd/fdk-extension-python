"""Webhook utility."""
import hashlib
import hmac
import re

import ujson

from sanic_boilerplate.constants import ASSOCIATION_CRITERIA
from sanic_boilerplate.constants import TEST_WEBHOOK_EVENT_NAME
from sanic_boilerplate.exceptions import FdkInvalidHMacError
from sanic_boilerplate.exceptions import FdkInvalidWebhookConfig
from sanic_boilerplate.exceptions import FdkWebhookHandlerNotFound
from sanic_boilerplate.exceptions import FdkWebhookProcessError
from sanic_boilerplate.exceptions import FdkWebhookRegistrationError
from sanic_boilerplate.utilities.logger import get_logger

logger = get_logger()


class WebhookRegistry:
    def __init__(self):
        self._handler_map = None
        self._config = None
        self._fdk_config = None

    def initialize(self, config, fdk_config):
        email_regex_match = r"^\S+@\S+\.\S+$"
        if not config.get("notification_email") or not re.findall(email_regex_match, config["notification_email"]):
            raise FdkInvalidWebhookConfig("Invalid or missing notification_email")

        if not config.get("api_path") or config["api_path"][0] != "/":
            raise FdkInvalidWebhookConfig("Invalid or missing api_path")

        if not config.get("event_map"):
            raise FdkInvalidWebhookConfig("Invalid or missing event_map")

        config["subscribe_on_install"] = True if not config.get("subscribe_on_install") else config[
            "subscribe_on_install"]
        self._handler_map = {}
        self._config = config
        self._fdk_config = fdk_config

        for event_name, handler_data in self._config["event_map"].items():
            self._handler_map[event_name] = handler_data

        logger.debug("Webhook registry initialized")

    def is_initialized(self):
        return self._handler_map and self._config["subscribe_on_install"]

    @staticmethod
    def _get_event_id_map(events):
        event_id_map = {}
        for each_event in events:
            event_id_map[
                each_event["event_category"] + "/" + each_event["event_name"] + "/" + each_event["event_type"] + "/" +
                each_event["version"]] = each_event["id"]
        return event_id_map

    def _association_criteria(self, application_id_list):
        if self._config["subscribed_saleschannel"] == "specific":
            return ASSOCIATION_CRITERIA["SPECIFIC"] if application_id_list else ASSOCIATION_CRITERIA["EMPTY"]
        return ASSOCIATION_CRITERIA["ALL"]

    @property
    def _webhook_url(self):
        return self._fdk_config['base_url'] + self._config["api_path"]

    def _is_config_updated(self, subscriber_config):
        updated = False
        config_criteria = self._association_criteria(subscriber_config["association"]["application_id"])
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

        if self._webhook_url != subscriber_config["webhook_url"]:
            logger.debug(f"Webhook url updated from {subscriber_config['webhook_url']} to {self._webhook_url}")
            subscriber_config["webhook_url"] = self._webhook_url
            updated = True

        return updated

    async def sync_events(self, platform_client, config=None, enable_webhooks=None):
        logger.debug("Sync events started")
        if config:
            self.initialize(config, self._fdk_config)

        events_map_response = await platform_client.webhook.fetchAllEventConfigurations()
        # TODO: replace with validate API
        events_map = events_map_response["json"]
        response = await platform_client.webhook.getSubscribersByExtensionId(
            extension_id=self._fdk_config["api_key"])
        if response["status_code"] != 200:
            raise FdkWebhookRegistrationError(f"Failed to getSubscribersByExtensionId with api response: "
                                              f"{response['content']}")
        subscriber_config = response["json"]

        events_map = self._get_event_id_map(events_map["event_configs"])

        register_new = False
        config_updated = False
        existing_events = []
        subscriber_config = subscriber_config["items"]
        if not subscriber_config:
            subscriber_config = {
                "name": self._fdk_config["api_key"],
                "webhook_url": self._webhook_url,
                "association": {
                    "company_id": platform_client._conf.companyId,
                    "application_id": [],
                    "criteria": self._association_criteria([])
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
            subscriber_config = subscriber_config[0]
            logger.debug(
                f"Webhook config on platform side for company id {platform_client._conf.companyId}: "
                f"{ujson.dumps(subscriber_config)}")
            subscriber_config["event_id"] = []
            event_configs = subscriber_config["event_configs"]
            existing_events = [each_event["id"] for each_event in event_configs]
            if enable_webhooks is not None:
                subscriber_config["status"] = "active" if enable_webhooks else "inactive"
                config_updated = True
            if self._is_config_updated(subscriber_config):
                config_updated = True

        for event_name in self._handler_map.keys():
            event_name = event_name + "/" + self._handler_map[event_name]["version"]
            if events_map.get(event_name):
                subscriber_config["event_id"].append(events_map[event_name])

        try:
            if register_new:
                response = await platform_client.webhook.registerSubscriberToEvent(body=subscriber_config)
                if self._fdk_config["debug"]:
                    if response["status_code"] != 200:
                        raise FdkWebhookRegistrationError(f"Failed to register subscriber to event with api response: "
                                                          f"{response['content']}")
                    event_map = {events_map[each_event_name]: each_event_name for each_event_name in events_map.keys()}
                    subscriber_config["event_id"] = [event_map[event_id] for event_id in subscriber_config["event_id"]]
                    logger.debug(f"Webhook config registered for company: {platform_client._conf.companyId}, "
                                 f"config: {ujson.dumps(subscriber_config)}")
            else:
                event_diff = [each_event_id for each_event_id in subscriber_config["event_id"]
                              if each_event_id not in existing_events]
                event_diff.extend([each_event_id for each_event_id in existing_events
                                   if each_event_id not in subscriber_config["event_id"]])

                if event_diff or config_updated:
                    response = await platform_client.webhook.updateSubscriberConfig(body=subscriber_config)
                    if self._fdk_config.get("debug"):
                        if response["status_code"] != 200:
                            raise FdkWebhookRegistrationError(f"Failed to update subscriber config with api response: "
                                                              f"{response['content']}")
                        event_map = {events_map[each_event_name]: each_event_name for each_event_name in
                                     events_map.keys()}
                        subscriber_config["event_id"] = [event_map[event_id] for event_id in
                                                         subscriber_config["event_id"]]
                        logger.debug(f"Webhook config updated for company: {platform_client._conf.companyId}, "
                                     f"config: {ujson.dumps(subscriber_config)}")

        except Exception as e:
            raise FdkWebhookRegistrationError(f"Failed to sync webhook events. Reason: {str(e)}")

    async def enable_sales_channel_webhook(self, platform_client, application_id):
        if self._config["subscribed_saleschannel"] != "specific":
            raise FdkWebhookRegistrationError("'subscribed_saleschannel' is not set to 'specific' in webhook config")
        try:
            response = await platform_client.webhook.getSubscribersByExtensionId(
                extension_id=self._fdk_config["api_key"])
            if response["status_code"] != 200:
                raise FdkWebhookRegistrationError(f"Failed to getSubscribersByExtensionId with api response: "
                                                  f"{response['content']}")
            subscriber_config = response["json"]
            subscriber_config = subscriber_config["items"][0]
            if not subscriber_config:
                raise FdkWebhookRegistrationError("Subscriber config not found")
            event_configs = subscriber_config["event_configs"]
            subscriber_config["event_id"] = [each_event["id"] for each_event in event_configs]
            arr_application_id = subscriber_config["association"].get("application_id") or []
            if application_id not in arr_application_id:
                arr_application_id.append(application_id)
                subscriber_config["association"]["application_id"] = arr_application_id
                subscriber_config["association"]["criteria"] = self._association_criteria(
                    subscriber_config["association"].get("application_id", []))
                response = await platform_client.webhook.updateSubscriberConfig(body=subscriber_config)
                if response["status_code"] == 200:
                    logger.debug(f"Webhook enabled for saleschannel: {application_id}")
                else:
                    raise FdkWebhookRegistrationError(
                        f"Failed to add saleschannel webhook with api response: {response['content']}")
        except Exception as e:
            raise FdkWebhookRegistrationError(f"Failed to add saleschannel webhook. Reason: {str(e)}")

    async def disable_sales_channel_webhook(self, platform_client, application_id):
        if self._config["subscribed_saleschannel"] != "specific":
            raise FdkWebhookRegistrationError("`subscribed_saleschannel` is not set to `specific` in webhook config")
        try:
            response = await platform_client.webhook.getSubscribersByExtensionId(
                extension_id=self._fdk_config["api_key"])
            if response["status_code"] != 200:
                raise FdkWebhookRegistrationError(
                    f"Failed to add saleschannel webhook with api response: {response['content']}")
            subscriber_config = response["json"]
            subscriber_config = subscriber_config["items"][0]
            if not subscriber_config:
                raise FdkWebhookRegistrationError("Subscriber config not found")
            event_configs = subscriber_config["event_configs"]
            subscriber_config["event_id"] = [each_event["id"] for each_event in event_configs]
            arr_application_id = subscriber_config["association"].get("application_id") or []
            if application_id in arr_application_id:
                arr_application_id.remove(application_id)
                subscriber_config["association"]["criteria"] = self._association_criteria(
                    subscriber_config["association"].get("application_id", []))
                subscriber_config["association"]["application_id"] = arr_application_id
                await platform_client.webhook.updateSubscriberConfig(body=subscriber_config)
                logger.debug(f"Webhook disabled for saleschannel: {application_id}")
        except Exception as e:
            raise FdkWebhookRegistrationError(f"Failed to disabled saleschannel webhook. Reason: {str(e)}")

    def verify_signature(self, request):
        req_signature = request.headers['x-fp-signature']
        calculated_signature = hmac.new(self._fdk_config["api_secret"].encode(),
                                        request.body,
                                        hashlib.sha256).hexdigest()
        if req_signature != calculated_signature:
            raise FdkInvalidHMacError("Signature passed does not match calculated body signature")

    async def process_webhook(self, request):
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
            if type(ext_handler) == "function":
                logger.debug(f"Webhook event received for company: {body['company_id']}, "
                             f"application: {body.get('application_id', '')}, event name: {event_name} ")
                await ext_handler(event_name, body, body["company_id"], body["application_id"])
            else:
                raise FdkWebhookHandlerNotFound(f"Webhook handler not assigned: {category_event_name}")
        except Exception as e:
            raise FdkWebhookProcessError(str(e))

    async def get_subscribe_config(self, platform_client, request):
        subscriber_config = await platform_client.webhook.getSubscribersByExtensionId(self._fdk_config["api_key"])
        return subscriber_config["items"][0]