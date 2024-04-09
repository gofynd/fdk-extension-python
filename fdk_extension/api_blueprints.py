from typing import Tuple, Optional, Union

from sanic import Blueprint
from sanic.blueprint_group import BlueprintGroup

from .middleware.api_middleware import application_proxy_on_request, platform_api_on_request, partner_api_on_request
from .middleware.session_middleware import session_middleware, partner_session_middleware


class ClientBlueprintGroup(BlueprintGroup):

    def __init__(self, client_type: Optional[str] = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client_type = client_type

    def append(self, value: Union[Blueprint, BlueprintGroup], *args, **kwargs) -> None:
        super().append(value)

        def chain(nested):
            for i in nested:
                if isinstance(i, BlueprintGroup):
                    yield from chain(i)
                else:
                    yield i

        # check and register middleware if not already exist
        for bp in chain(value):
            middleware_function = [i.middleware.func for i in bp._future_middleware]

            if self.client_type == "platform":
                if session_middleware not in middleware_function:
                    bp.middleware(session_middleware, "request", *args, **kwargs)

                if platform_api_on_request not in middleware_function:
                    bp.middleware(platform_api_on_request, "request", *args, **kwargs)

            elif self.client_type == "partner":
                if partner_session_middleware not in middleware_function:
                    bp.middleware(partner_session_middleware, "request", *args, **kwargs)

                if partner_api_on_request not in middleware_function:
                    bp.middleware(partner_api_on_request, "request", *args, **kwargs)

            elif self.client_type == "application":
                if application_proxy_on_request not in middleware_function:
                    bp.middleware(application_proxy_on_request, "request", *args, **kwargs)


def setup_proxy_routes() -> Tuple[BlueprintGroup, BlueprintGroup]:
    platform_api_routes = ClientBlueprintGroup(client_type="platform")
    application_proxy_routes = ClientBlueprintGroup(client_type="application")
    partner_proxy_routes = ClientBlueprintGroup(client_type="partner")

    return platform_api_routes, application_proxy_routes, partner_proxy_routes
