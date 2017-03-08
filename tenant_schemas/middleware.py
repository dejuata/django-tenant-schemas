import django

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import DisallowedHost
from django.db import connection
from django.http import Http404
from tenant_schemas.utils import (get_tenant_model, remove_www,
                                  get_public_schema_name)

if django.VERSION >= (1, 10, 0):
    MIDDLEWARE_MIXIN = django.utils.deprecation.MiddlewareMixin
else:
    MIDDLEWARE_MIXIN = object

"""
These middlewares should be placed at the very top of the middleware stack.
Selects the proper database schema using request information. Can fail in
various ways which is better than corrupting or revealing data.

Extend BaseTenantMiddleware for a custom tenant selection strategy,
such as inspecting the header, or extracting it from some OAuth token.
"""

class BaseTenantMiddleware(MIDDLEWARE_MIXIN):

    """
    Subclass and override  this to achieve desired behaviour. Given a
    request, return the tenant to use. Tenant should be an instance
    of TENANT_MODEL.
    """
    def get_tenant(self, request):
        raise NotImplementedError


    def process_request(self, request):
        # Connection needs first to be at the public schema, as this is where
        # the tenant metadata is stored.
        connection.set_schema_to_public()

        # get_tenant must be implemented by extending this class.
        tenant = self.get_tenant(request)

        if tenant is None:
            raise Exception("Trying to set current tenant to None!")

        request.tenant = tenant
        connection.set_tenant(request.tenant)

        # Content type can no longer be cached as public and tenant schemas
        # have different models. If someone wants to change this, the cache
        # needs to be separated between public and shared schemas. If this
        # cache isn't cleared, this can cause permission problems. For example,
        # on public, a particular model has id 14, but on the tenants it has
        # the id 15. if 14 is cached instead of 15, the permissions for the
        # wrong model will be fetched.
        ContentType.objects.clear_cache()

        # Do we have a public-specific urlconf?
        if hasattr(settings, 'PUBLIC_SCHEMA_URLCONF') and request.tenant.schema_name == get_public_schema_name():
            request.urlconf = settings.PUBLIC_SCHEMA_URLCONF

class TenantMiddleware(BaseTenantMiddleware):
    """
    Selects the proper database schema using the request host. E.g. <my_tenant>.<my_domain>
    """
    TENANT_NOT_FOUND_EXCEPTION = Http404

    def hostname_from_request(self, request):
        """ Extracts hostname from request. Used for custom requests filtering.
            By default removes the request's port and common prefixes.
        """
        return remove_www(request.get_host().split(':')[0]).lower()

    def get_tenant(self, request):
        hostname = self.hostname_from_request(request)

        TenantModel = get_tenant_model()
        try:
            return TenantModel.objects.get(domain_url=hostname)
        except TenantModel.DoesNotExist:
            raise self.TENANT_NOT_FOUND_EXCEPTION(
                'No tenant for hostname "%s"' % hostname)


class SuspiciousTenantMiddleware(TenantMiddleware):
    """
    Extend the TenantMiddleware in scenario where you need to configure
    ``ALLOWED_HOSTS`` to allow ANY domain_url to be used because your tenants
    can bring any custom domain with them, as opposed to all tenants being a
    subdomain of a common base.

    See https://github.com/bernardopires/django-tenant-schemas/pull/269 for
    discussion on this middleware.
    """
    TENANT_NOT_FOUND_EXCEPTION = DisallowedHost


class DefaultTenantMiddleware(SuspiciousTenantMiddleware):
    """
    Extend the SuspiciousTenantMiddleware in scenario where you want to
    configure a tenant to be served if the hostname does not match any of the
    existing tenants.

    Subclass and override DEFAULT_SCHEMA_NAME to use a schema other than the
    public schema.

        class MyTenantMiddleware(DefaultTenantMiddleware):
            DEFAULT_SCHEMA_NAME = 'default'
    """
    DEFAULT_SCHEMA_NAME = None

    def get_tenant(self, request):
        try:
            return super(DefaultTenantMiddleware, self).get_tenant(request)
        except self.TENANT_NOT_FOUND_EXCEPTION as e:
            schema_name = self.DEFAULT_SCHEMA_NAME
            if not schema_name:
                schema_name = get_public_schema_name()

            TenantModel = get_tenant_model()
            try:
                return TenantModel.objects.get(schema_name=schema_name)
            except TenantModel.DoesNotExist:
                raise e # Raise the same exception (we don't have a tenant)
