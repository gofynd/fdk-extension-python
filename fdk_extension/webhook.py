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
        self._event_map = None
        self._config : dict = None
        self._fdk_config : dict = None

    async def initialize(self, config: dict, fdk_config: dict):
        email_regex_match = r"^\S+@\S+\.\S+$"
        if not config.get("notification_email") or not re.findall(email_regex_match, config["notification_email"]):
            raise FdkInvalidWebhookConfig("Invalid or missing notification_email")

        if not config.get("api_path") or config["api_path"][0] != "/":
            raise FdkInvalidWebhookConfig("Invalid or missing api_path")
        
        if config.get("marketplace") == True and config.get("subscribed_saleschannel") != "specific":
            raise FdkInvalidWebhookConfig("marketplace is only allowed when subscribed_saleschannel is 'specific'")

        if not config.get("event_map"):
            raise FdkInvalidWebhookConfig("Invalid or missing event_map")

        config["subscribe_on_install"] = config.get("subscribe_on_install", True)
        self._event_map = {
            'rest': {},
            'kafka': {},
            'pub_sub': {},
            'sqs': {},
            'event_bridge': {},
            'temporal': {}
        }
        self._config = config
        self._fdk_config = fdk_config

        allowed_providers = ['kafka', 'rest', 'pub_sub', 'temporal', 'sqs', 'event_bridge']

        for event_name, event_data in self._config['event_map'].items():
            if len(event_name.split('/')) != 3:
                raise FdkInvalidWebhookConfig(f'Invalid webhook event map key. Invalid key: {event_name}')
            
            if 'version' not in event_data:
                raise FdkInvalidWebhookConfig(f'Missing version in webhook event {event_name}')
            
            event_data['provider'] = event_data.get('provider', 'rest')
            
            if event_data['provider'] not in allowed_providers:
                raise FdkInvalidWebhookConfig(
                    f'Invalid provider value in webhook event {event_name}, allowed values are {", ".join(allowed_providers)}'
                )
            
            if event_data['provider'] == 'rest' and 'handler' not in event_data:
                raise FdkInvalidWebhookConfig(f'Missing handler in webhook event {event_name}')
            elif event_data['provider'] in ['kafka', 'pub_sub'] and 'topic' not in event_data:
                raise FdkInvalidWebhookConfig(f'Missing topic in webhook event {event_name}')
            elif event_data['provider'] in ['temporal', 'sqs'] and 'queue' not in event_data:
                raise FdkInvalidWebhookConfig(f'Missing queue in webhook event {event_name}')
            elif event_data['provider'] == 'temporal' and 'workflow_name' not in event_data:
                raise FdkInvalidWebhookConfig(f'Missing workflow_name in webhook event {event_name}')
            elif event_data['provider'] == 'event_bridge' and 'event_bridge_name' not in event_data:
                raise FdkInvalidWebhookConfig(f'Missing event_bridge_name in webhook event {event_name}')
            
            self._event_map[event_data['provider']][f"{event_name}/v{event_data['version']}"] = event_data

        all_event_map = {**self._event_map['rest'], **self._event_map['kafka'],
                         **self._event_map['sqs'], **self._event_map['pub_sub'],
                         **self._event_map['temporal'], **self._event_map['event_bridge']}
        

        await self.get_event_config(all_event_map)
        event_config["events_map"] = self.__get_event_id_map(event_config.get("event_configs"))
        self.__validate_events_map(all_event_map)

        if len(event_config["event_not_found"]):
            errors = []
            for key in event_config["event_not_found"]:
                errors.append(ujson.dumps({"name": key, "version": event_config["event_not_found"][key]}))

            raise FdkInvalidWebhookConfig(f"Webhooks events {', '.join(errors)} not found")
        
        logger.debug('Webhook registry initialized')

    @property
    def is_initialized(self) -> bool:
        return self._event_map and self._config["subscribe_on_install"]


    def __validate_events_map(self, handler_config: dict):
        event_config.pop("event_not_found", None)
        event_config["event_not_found"] = {}

        for key in handler_config.keys():
            if not f"{key}" in event_config["events_map"]:
                event_config["event_not_found"][key] = handler_config[key]["version"]


    def __get_event_id_map(self, events: list) -> dict:
        event_map = {}
        for event in events:
            event_map[f"{event['event_category']}/{event['event_name']}/{event['event_type']}/v{event['version']}"] = event['id']
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

        if config_criteria == ASSOCIATION_CRITERIA["SPECIFIC"]:
            if subscriber_config.get('type') == 'marketplace' and not self._config['marketplace']:
                logger.debug(f"Type updated from {subscriber_config.get('type')} to None")
                subscriber_config['type'] = None
                updated = True
            elif (not subscriber_config.get('type') or subscriber_config.get('type') != "marketplace") and self._config['marketplace']:
                logger.debug(f"Type updated from {subscriber_config.get('type')} to marketplace")
                subscriber_config['type'] = "marketplace"
                updated = True
        else:
            if subscriber_config.get('type') == 'marketplace':
                logger.debug(f"Type updated from {subscriber_config.get('type')} to None")
                subscriber_config['type'] = None
                updated = True

        return updated

    async def sync_events(self, platform_client: PlatformClient, config: dict=None, enable_webhooks: bool=None):
        if config:
            await self.initialize(config, self._fdk_config)
        
        if not self.is_initialized:
            raise FdkInvalidWebhookConfig('Webhook registry not initialized')
        
        logger.debug('Webhook sync events started')
        
        subscriber_config_list = await self.get_subscriber_config(platform_client)

        subscriber_synced_for_all_providers = await self.sync_subscriber_config_for_all_providers(platform_client, subscriber_config_list)

        if not subscriber_synced_for_all_providers:
            subscriber_config_list = await self.get_subscriber_config(platform_client)
            await self.sync_subscriber_config(
                subscriber_config_list.get('rest', {}), 'rest', self._event_map.get('rest',{}), platform_client, enable_webhooks
            )
            await self.sync_subscriber_config(
                subscriber_config_list.get('kafka', {}), 'kafka', self._event_map.get('kafka',{}), platform_client, enable_webhooks
            )
            await self.sync_subscriber_config(
                subscriber_config_list.get('pub_sub', {}), 'pub_sub', self._event_map.get('pub_sub',{}), platform_client, enable_webhooks
            )
            await self.sync_subscriber_config(
                subscriber_config_list.get('sqs', {}), 'sqs', self._event_map.get('sqs',{}), platform_client, enable_webhooks
            )
            await self.sync_subscriber_config(
                subscriber_config_list.get('event_bridge', {}), 'event_bridge', self._event_map.get('event_bridge',{}), platform_client, enable_webhooks
            )
            await self.sync_subscriber_config(
                subscriber_config_list.get('temporal', {}), 'temporal', self._event_map.get('temporal',{}), platform_client, enable_webhooks
            )
            


    async def sync_subscriber_config_for_all_providers(self, platform_client, subscriber_config_list):
        payload = self.create_register_payload_data(subscriber_config_list)
        token = await platform_client.config.oauthClient.getAccessToken()
        try:
            url = f"{self._fdk_config.get('cluster')}/service/platform/webhook/v3.0/company/{platform_client.config.companyId}/subscriber"
            headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
            response = await retry_middleware(AiohttpHelper().aiohttp_request, request_type="PUT", url=url, data=payload, headers=headers)
            return True
        except Exception as err:
            if err.code != '404':
                raise err
            return False
        
    def create_register_payload_data(self, subscriber_config_list):
        payload = {
            "webhook_config": {
                "notification_email": self._config['notification_email'],
                "name": self._fdk_config['api_key'],
                "association": {
                    "extension_id": self._fdk_config['api_key'],
                    "application_id": [],
                    "criteria": self.__association_criteria([])
                },
                "status": "active",
                "event_map": {}
            }
        }

        # Check the first subscriber's criteria if sales channel is 'specific'
        config_keys = list(subscriber_config_list.keys())
        if self._config['subscribed_saleschannel'] == 'specific' and config_keys:
            first_config = subscriber_config_list[config_keys[0]]
            if first_config.get("association", {}).get("criteria") == ASSOCIATION_CRITERIA["SPECIFIC"]:
                payload["webhook_config"]["association"] = first_config["association"]

        payload_event_map = payload["webhook_config"]["event_map"]

        for key, event in self._config['event_map'].items():
            if event['provider'] not in payload_event_map:
                payload_event_map[event['provider']] = {}

                if payload["webhook_config"].get("association", {}).get("criteria") == ASSOCIATION_CRITERIA["SPECIFIC"]:
                    payload_event_map[event['provider']] = {
                        "type": 'marketplace' if self._config['marketplace'] else None
                    }

                payload_event_map[event['provider']]["events"] = []
                
                if event['provider'] == 'rest':
                    payload_event_map[event['provider']].update({
                        "webhook_url": self.__webhook_url,
                        "auth_meta": {
                            "type": "hmac",
                            "secret": self._fdk_config['api_secret']
                        }
                    })

            event_data = {
                "event_category": key.split('/')[0],
                "event_name": key.split('/')[1],
                "event_type": key.split('/')[2],
                "version": event.get('version'),
                "topic": event.get('topic'),
                "queue": event.get('queue'),
                "workflow_name": event.get('workflow_name'),
                "event_bridge_name": event.get('event_bridge_name')
            }
            payload_event_map[event['provider']]["events"].append(event_data)

        return payload

    async def sync_subscriber_config(self, subscriber_config, config_type, current_event_map_config, platform_client, enable_webhooks):
        register_new = False
        config_updated = False
        existing_events = []

        if not subscriber_config:
            subscriber_config = {
                "name": self._fdk_config.get("api_key"),
                "association": {
                    "company_id": platform_client.config.companyId,
                    "application_id": [],
                    "criteria": self.__association_criteria([])
                },
                "status": "active",
                "auth_meta": {
                    "type": "hmac",
                    "secret": self._fdk_config.get('api_secret')
                },
                "events": [],
                "provider": config_type,
                "email_id": self._config.get('notification_email')
            }
            if config_type == 'rest':
                subscriber_config['webhook_url'] = self.__webhook_url
            register_new = True
            if enable_webhooks is not None:
                subscriber_config['status'] = 'active' if enable_webhooks else 'inactive'
        else:
            logger.debug(f"Webhook {config_type} config on platform side for company id {platform_client.config.companyId}: {subscriber_config}")

            id = subscriber_config.get('id')
            name = subscriber_config.get('name')
            webhook_url = subscriber_config.get('webhook_url')
            provider = subscriber_config.get('provider', 'rest')
            association = subscriber_config.get('association')
            status = subscriber_config.get('status')
            auth_meta = subscriber_config.get('auth_meta')
            event_configs = subscriber_config.get('event_configs', [])
            email_id = subscriber_config.get('email_id')
            _type = subscriber_config.get('type')

            subscriber_config = {
                'id': id,
                'name': name,
                'webhook_url': webhook_url,
                'provider': provider,
                'association': association,
                'status': status,
                'auth_meta': auth_meta,
                'email_id': email_id,
                'type': _type
            }
            subscriber_config['events'] = []
            existing_event = []
            for event in subscriber_config.get('event_configs', []):
                eventToAdd = {}
                eventToAdd["slug"] = f"{event.get('event_category')}/{event.get('event_name')}/{event.get('event_type')}/v{event.get('version')}"

                if(event.get('subscriber_event_mapping') and event.get('subscriber_event_mapping').get('broadcaster_config')):
                    broadcaster_config = event['subscriber_event_mapping']['broadcaster_config']

                    eventToAdd.update({
                        'topic': broadcaster_config.get('topic', ''),
                        'queue': broadcaster_config.get('queue', ''),
                        'event_bridge_name': broadcaster_config.get('event_bridge_name', ''),
                        'workflow_name': broadcaster_config.get('workflow_name', '')
                    })
                
                existing_events.append(eventToAdd)
                
        

            # Check for configuration updates
            if config_type == 'rest' and subscriber_config.get('auth_meta', {}).get('secret') != self._fdk_config.get('api_secret'):
                subscriber_config['auth_meta']['secret'] = self._fdk_config.get('api_secret')
                config_updated = True

            if enable_webhooks is not None:
                new_status = 'active' if enable_webhooks else 'inactive'
                if new_status != subscriber_config.get('status'):
                    subscriber_config['status'] = new_status
                    config_updated = True

            if self.__is_config_updated(subscriber_config):
                config_updated = True

        # Adding all events to subscriberConfig if valid
        for event_name in current_event_map_config:
            event_id = event_config.get("events_map", {}).get(event_name)
            if event_id:
                event = {
                    'slug': event_name,
                    'topic': current_event_map_config.get(event_name, {}).get('topic'),
                    'queue': current_event_map_config.get(event_name, {}).get('queue'),
                    'event_bridge_name': current_event_map_config.get(event_name, {}).get('event_bridge_name'),
                    'workflow_name': current_event_map_config.get(event_name, {}).get('workflow_name')
                }
                subscriber_config['events'].append(event)

        try:
            if register_new:
                if not subscriber_config['events']:
                    logger.debug(f"Skipped registerSubscriber API call as no {config_type} based events found")
                    return
                await self.register_subscriber_config(platform_client, subscriber_config)
                if self._fdk_config.get('debug'):
                    subscriber_config['events'] = [event['slug'] for event in subscriber_config['events']]
                    logger.debug(f"Webhook {config_type} config registered for company: {platform_client.config.companyId}, config: {subscriber_config}")
            else:
                event_diff = [
                    *[
                        event for event in subscriber_config['events'] 
                        if not any(item['slug'] == event['slug'] for item in existing_events)
                    ],
                    *[
                        event for event in existing_events 
                        if not any(event['slug'] == item['slug'] for item in subscriber_config['events'])
                    ]
                ]

                # Check if these keys have changed
                config_type_keys_to_check = {
                    'kafka': ['topic'],
                    'pub_sub': ['topic'],
                    'temporal': ['queue', 'workflow_name'],
                    'sqs': ['queue'],
                    'event_bridge': ['event_bridge_name']
                }

                if config_type != 'rest' and not config_updated:
                    for event in subscriber_config['events']:
                        existing_event = next((e for e in existing_events if e['slug'] == event['slug']), None)
                        if existing_event:
                            for key in config_type_keys_to_check.get(config_type, []):
                                if event.get(key) != existing_event.get(key):
                                    config_updated = True
                                    break

                if event_diff or config_updated:
                    await self.update_subscriber_config(platform_client, subscriber_config)
                    if self._fdk_config.get('debug'):
                        subscriber_config['events'] = [event['slug'] for event in subscriber_config['events']]
                        logger.debug(f"Webhook {config_type} config updated for company: {platform_client.config.companyId}, config: {subscriber_config}")
        except Exception as ex:
            raise FdkWebhookRegistrationError(f"Failed to sync webhook {config_type} events. Reason: {ex}")


    async def register_subscriber_config(self, platform_client, subscriber_config):
        token = await platform_client.config.oauthClient.getAccessToken()

        try:
            url = f"{self._fdk_config['cluster']}/service/platform/webhook/v2.0/company/{platform_client.config.companyId}/subscriber"
            headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
            response = await retry_middleware(AiohttpHelper().aiohttp_request, request_type="POST", url=url, data=subscriber_config, headers=headers)

        except Exception as err:
            if subscriber_config['provider'] != "rest":
                logger.debug(f"Webhook Subscriber Config type {subscriber_config['provider']} is not supported with the current FP version")
                return

            if err.code != '404':
                raise err

            events_list = subscriber_config['events']
            del subscriber_config['events']
            provider = subscriber_config['provider']
            del subscriber_config['provider']
            subscriber_config['event_id'] = [event_config.events_map[event['slug']] for event in events_list]

            url = f"{self._fdk_config['cluster']}/service/platform/webhook/v1.0/company/{platform_client.config.companyId}/subscriber"
            headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
            response = await retry_middleware(AiohttpHelper().aiohttp_request, request_type="POST", url=url, data=subscriber_config, headers=headers)
            try:
                response = await retry_middleware(AiohttpHelper().aiohttp_request, request_type="POST", url=url, data=subscriber_config, headers=headers)
                subscriber_config['events'] = events_list
                subscriber_config['provider'] = provider
                return response
            except Exception as fallback_err:
                raise FdkWebhookRegistrationError(
                    f"Error while registering webhook subscriber configuration with fallback, Reason: {fallback_err}"
                )
            
    async def update_subscriber_config(self, platform_client, subscriber_config):
        token = await platform_client.config.oauthClient.getAccessToken()

        try:
            # Set status to 'inactive' and remove events if no events are present
            if not subscriber_config.get('events'):
                subscriber_config['status'] = 'inactive'
                subscriber_config.pop('events', None)

            try:
                # Attempt to update with v2.0 API
                url = f"{self._fdk_config['cluster']}/service/platform/webhook/v2.0/company/{platform_client.config.companyId}/subscriber"
                headers = {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"
                    }
                response = await retry_middleware(AiohttpHelper().aiohttp_request, request_type="PUT", url=url, data=subscriber_config, headers=headers)
                return response

            except Exception as err:
                if subscriber_config['provider'] != "rest":
                    logger.debug(f"Webhook Subscriber Config type {subscriber_config['provider']} is not supported with the current FP version")
                    return
                
                if getattr(err, 'code', None) != '404':
                    raise err
                
                # Prepare for fallback to v1.0 API
                events_list = subscriber_config.pop('events', [])
                provider = subscriber_config.pop('provider', None)
                subscriber_config['event_id'] = [event_config.events_map[event['slug']] for event in events_list]


                url = f"{self._fdk_config['cluster']}/service/platform/webhook/v1.0/company/{platform_client.config.companyId}/subscriber"
                headers = {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"
                    }
                response = await retry_middleware(AiohttpHelper().aiohttp_request, request_type="PUT", url=url, data=subscriber_config, headers=headers)
                subscriber_config['events'] = events_list
                subscriber_config['provider'] = provider
                return response

        except Exception as err:
            raise FdkWebhookRegistrationError(f"Error while updating webhook subscriber configuration. Reason: {err}")
        

    async def enable_sales_channel_webhook(self, platform_client: PlatformClient, application_id: str):
        if not self.is_initialized:
            raise FdkInvalidWebhookConfig("Webhook registry not initialized")

        if self._config.get("subscribed_saleschannel") != "specific":
            raise FdkWebhookRegistrationError("'subscribed_saleschannel' is not set to 'specific' in webhook config")
        
        try:
            subscriber_config_list = await self.get_subscriber_config(platform_client)

            if len(subscriber_config_list) == 0:
              raise FdkWebhookRegistrationError("Subscriber config not found")

            for subscriber_config in subscriber_config_list.values():
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
                    if subscriber_config.get('association', {}).get('criteria') == ASSOCIATION_CRITERIA["SPECIFIC"]:
                        subscriber_config['type'] = 'marketplace' if self._config['marketplace'] else None
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
            subscriber_config_list = await self.get_subscriber_config(platform_client)
            if len(subscriber_config_list) == 0:
              raise FdkWebhookRegistrationError("Subscriber config not found")

            for subscriber_config in subscriber_config_list.values():
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
                    if subscriber_config.get('association', {}).get('criteria') == ASSOCIATION_CRITERIA["SPECIFIC"]:
                        subscriber_config['type'] = 'marketplace' if self._config['marketplace'] else None
                    else:
                        subscriber_config['type'] = None
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
            event = body.get('event', {})
            if event.get('name') == TEST_WEBHOOK_EVENT_NAME:
                return
            self.verify_signature(request)

            event_name = f"{event.get('category', '')}/{event.get('name', '')}/{event.get('type', '')}/v{event.get('version', '')}"
            event_handler_map = self._event_map['rest'].get(event_name, {})
            ext_handler = event_handler_map.get('handler')

            if callable(ext_handler):
                logger.debug(f"Webhook event received for company: {body['company_id']}, "
                             f"application: {body.get('application_id', '')}, event name: {event_name} ")
                await ext_handler(event_name, body, body.get("company_id", ''),body.get("application_id", ''))
            else:
                raise FdkWebhookHandlerNotFound(f"Webhook handler not assigned: {event_name}")
        except Exception as e:
            raise FdkWebhookProcessError(str(e))
        
    async def get_subscriber_config(self, platform_client):
        token = await platform_client.config.oauthClient.getAccessToken()

        try:
            path = f"/service/platform/webhook/v1.0/company/{platform_client.config.companyId}/extension/{self._fdk_config.get('api_key')}/subscriber"

            url =  f"{self._fdk_config.get('cluster')}/{path}"
            headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
            
            subscriber_config_response = await retry_middleware(AiohttpHelper().aiohttp_request, request_type="GET", url=url, headers=headers)
            print(subscriber_config_response["json"])

            # Organize the subscriber configurations by provider
            subscriber_config = {}
            for config in subscriber_config_response["json"]['items']:
                if config.get('provider'):
                    subscriber_config[config['provider']] = config

            return subscriber_config

        except Exception as err:
            # Handle errors by raising a custom exception
            raise FdkInvalidWebhookConfig(f"Error while fetching webhook subscriber configuration. Reason: {err}")



    async def get_event_config(self, config: dict) -> dict:
        try:
            data = []
            for key in config.keys():
                event_dict = {}
                event_details = key.split("/")
                event_dict["event_category"] = event_details[0]
                event_dict["event_name"] = event_details[1]
                event_dict["event_type"] = event_details[2]
                event_dict["version"] = config[key].get("version")
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
            response = await retry_middleware(AiohttpHelper().aiohttp_request, request_type="POST", url=url, data=data, headers=headers)
            response_data: dict = response["json"]
            event_config["event_configs"] = response_data.get("event_configs")
            logger.debug(f"Webhook events config received: {ujson.dumps(response_data)}")
            return response_data

        except Exception as e:
            raise FdkInvalidWebhookConfig(f"Error while fetching webhook events configuration, Reason: {str(e)}")