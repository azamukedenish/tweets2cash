# -*- coding: utf-8 -*-
"""
Provides various throttling policies.
"""
from __future__ import unicode_literals

import time

from django.core.cache import cache as default_cache
from django.core.exceptions import ImproperlyConfigured

from rest_framework.settings import api_settings

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from ipware.ip import get_ip
from netaddr import all_matching_cidrs
from netaddr.core import AddrFormatError



class BaseThrottle(object):
    """
    Rate throttling of requests.
    """
    response = None

    def allow_request(self, request, view, response=None):
        """
        Return `True` if the request should be allowed, `False` otherwise.
        """
        raise NotImplementedError('.allow_request() must be overridden')

    def get_ident(self, request):
        """
        Identify the machine making the request by parsing HTTP_X_FORWARDED_FOR
        if present and number of proxies is > 0. If not use all of
        HTTP_X_FORWARDED_FOR if it is available, if not use REMOTE_ADDR.
        """
        xff = request.META.get('HTTP_X_FORWARDED_FOR')
        remote_addr = request.META.get('REMOTE_ADDR')
        num_proxies = api_settings.NUM_PROXIES

        if num_proxies is not None:
            if num_proxies == 0 or xff is None:
                return remote_addr
            addrs = xff.split(',')
            client_addr = addrs[-min(num_proxies, len(addrs))]
            return client_addr.strip()

        return ''.join(xff.split()) if xff else remote_addr

    def wait(self):
        """
        Optionally, return a recommended number of seconds to wait before
        the next request.
        """
        return None


class SimpleRateThrottle(BaseThrottle):
    """
    A simple cache implementation, that only requires `.get_cache_key()`
    to be overridden.

    The rate (requests / seconds) is set by a `throttle` attribute on the View
    class.  The attribute is a string of the form 'number_of_requests/period'.

    Period should be one of: ('s', 'sec', 'm', 'min', 'h', 'hour', 'd', 'day')

    Previous request information used for throttling is stored in the cache.
    """
    cache = default_cache
    timer = time.time
    cache_format = 'throttle_%(scope)s_%(ident)s'
    scope = None
    THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES

    def __init__(self):
        if not getattr(self, 'rate', None):
            self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)

    def get_cache_key(self, request, view):
        """
        Should return a unique cache-key which can be used for throttling.
        Must be overridden.

        May return `None` if the request should not be throttled.
        """
        raise NotImplementedError('.get_cache_key() must be overridden')

    def get_rate(self):
        """
        Determine the string representation of the allowed request rate.
        """
        if not getattr(self, 'scope', None):
            msg = ("You must set either `.scope` or `.rate` for '%s' throttle" %
                   self.__class__.__name__)
            raise ImproperlyConfigured(msg)

        try:
            return self.THROTTLE_RATES[self.scope]
        except KeyError:
            msg = "No default throttle rate set for '%s' scope" % self.scope
            raise ImproperlyConfigured(msg)

    def parse_rate(self, rate):
        """
        Given the request rate string, return a two tuple of:
        <allowed number of requests>, <period of time in seconds>
        """
        if rate is None:
            return (None, None)
        num, period = rate.split('/')
        num_requests = int(num)
        duration = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[period[0]]
        return (num_requests, duration)

    def allow_request(self, request, view,response=None):
        """
        Implement the check to see if the request should be throttled.

        On success calls `throttle_success`.
        On failure calls `throttle_failure`.
        """
        if self.rate is None:
            return True

        self.key = self.get_cache_key(request, view)
        if self.key is None:
            return True

        self.history = self.cache.get(self.key, [])
        self.now = self.timer()

        # Drop any requests from the history which have now passed the
        # throttle duration
        while self.history and self.history[-1] <= self.now - self.duration:
            self.history.pop()
        if len(self.history) >= self.num_requests:
            return self.throttle_failure(response)
        return self.throttle_success(response)

    def throttle_success(self, response=None):
        """
        Inserts the current request's timestamp along with the key
        into the cache.
        """
        self.history.insert(0, self.now)
        self.cache.set(self.key, self.history, self.duration)
        return True

    def throttle_failure(self, response=None):
        """
        Called when a request to the API has failed due to throttling.
        """
        return False

    def wait(self):
        """
        Returns the recommended next request time in seconds.
        """
        if self.history:
            remaining_duration = self.duration - (self.now - self.history[-1])
        else:
            remaining_duration = self.duration

        available_requests = self.num_requests - len(self.history) + 1
        if available_requests <= 0:
            return None

        return remaining_duration / float(available_requests)


class AnonRateThrottle(SimpleRateThrottle):
    """
    Limits the rate of API calls that may be made by a anonymous users.

    The IP address of the request will be used as the unique cache key.
    """
    scope = 'anon'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated:
            return None  # Only throttle unauthenticated requests.

        return self.cache_format % {
            'scope': self.scope,
            'ident': self.get_ident(request)
        }


class UserRateThrottle(SimpleRateThrottle):
    """
    Limits the rate of API calls that may be made by a given user.

    The user id will be used as a unique cache key if the user is
    authenticated.  For anonymous requests, the IP address of the request will
    be used.
    """
    scope = 'user'

    def get_cache_key(self, request, view):
        if request.user.is_authenticated:
            ident = request.user.pk
        else:
            ident = self.get_ident(request)

        return self.cache_format % {
            'scope': self.scope,
            'ident': ident
        }


class ScopedRateThrottle(SimpleRateThrottle):
    """
    Limits the rate of API calls by different amounts for various parts of
    the API.  Any view that has the `throttle_scope` property set will be
    throttled.  The unique cache key will be generated by concatenating the
    user id of the request, and the scope of the view being accessed.
    """
    scope_attr = 'throttle_scope'

    def __init__(self):
        # Override the usual SimpleRateThrottle, because we can't determine
        # the rate until called by the view.
        pass

    def allow_request(self, request, view, response=None):
        # We can only determine the scope once we're called by the view.
        self.scope = getattr(view, self.scope_attr, None)

        # If a view does not have a `throttle_scope` always allow the request
        if not self.scope:
            return True

        # Determine the allowed request rate as we normally would during
        # the `__init__` call.
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)

        # We can now proceed as normal.
        return super(ScopedRateThrottle, self).allow_request(request, view, response)

    def get_cache_key(self, request, view):
        """
        If `view.throttle_scope` is not set, don't apply this throttle.

        Otherwise generate the unique cache key by concatenating the user id
        with the '.throttle_scope` property of the view.
        """
        if request.user.is_authenticated:
            ident = request.user.pk
        else:
            ident = self.get_ident(request)

        return self.cache_format % {
            'scope': self.scope,
            'ident': ident
        }

class GlobalThrottlingMixin:
    """
    Define the cache key based on the user IP independently if the user is
    logged in or not.
    """
    def get_cache_key(self, request, view):
        ident = get_ip(request)

        return self.cache_format % {
            "scope": self.scope,
            "ident": ident
        }


class ThrottleByActionMixin:
    throttled_actions = []

    def allow_request(self, request, view, response=None):

        if view.action in self.throttled_actions:
            return super().allow_request(request, view, response)
        return True


class CommonThrottle(SimpleRateThrottle):
    cache_format = "throtte_%(scope)s_%(rate)s_%(ident)s"

    def __init__(self):
        pass

    def is_whitelisted(self, ident):
        for whitelisted in settings.REST_FRAMEWORK['DEFAULT_THROTTLE_WHITELIST']:
            if isinstance(whitelisted, int) and whitelisted == ident:
                return True
            elif isinstance(whitelisted, str):
                try:
                    if all_matching_cidrs(ident, [whitelisted]) != []:
                        return True
                except(AddrFormatError, ValueError):
                    pass
        return False

    def allow_request(self, request, view, response=None):
        scope = self.get_scope(request)
        ident = self.get_ident(request)
        rates = self.get_rates(scope)

        if self.is_whitelisted(ident):
            return True

        if rates is None or rates == []:
            return True

        now = self.timer()

        waits = []
        history_writes = []

        for rate in rates:
            rate_name = rate[0]
            rate_num_requests = rate[1]
            rate_duration = rate[2]

            key = self.get_cache_key(ident, scope, rate_name)
            history = self.cache.get(key, [])

            while history and history[-1] <= now - rate_duration:
                history.pop()

            if len(history) >= rate_num_requests:
                waits.append(self.wait_time(history, rate, now))

            history_writes.append({
                "key": key,
                "history": history,
                "rate_duration": rate_duration,
            })

        if waits:
            self._wait = max(waits)
            return False

        for history_write in history_writes:
            history_write['history'].insert(0, now)
            self.cache.set(
                history_write['key'],
                history_write['history'],
                history_write['rate_duration']
            )
        return True

    def get_rates(self, scope):
        try:
            rates = self.THROTTLE_RATES[scope]
        except KeyError:
            msg = "No default throttle rate set for \"%s\" scope" % scope
            raise ImproperlyConfigured(msg)

        if rates is None:
            return []
        elif isinstance(rates, str):
            return [self.parse_rate(rates)]
        elif isinstance(rates, list):
            return list(map(self.parse_rate, rates))
        else:
            msg = "No valid throttle rate set for \"%s\" scope" % scope
            raise ImproperlyConfigured(msg)

    def parse_rate(self, rate):
        """
        Given the request rate string, return a two tuple of:
        <allowed number of requests>, <period of time in seconds>
        """
        if rate is None:
            return None
        num, period = rate.split("/")
        num_requests = int(num)
        duration = {"s": 1, "m": 60, "h": 3600, "d": 86400}[period[0]]
        return (rate, num_requests, duration)

    def get_scope(self, request):
        scope_prefix = "user" if request.user.is_authenticated() else "anon"
        scope_sufix = "write" if request.method in ["POST", "PUT", "PATCH", "DELETE"] else "read"
        scope = "{}-{}".format(scope_prefix, scope_sufix)
        return scope

    def get_ident(self, request):
        if request.user.is_authenticated():
            return request.user.id
        ident = get_ip(request)
        return ident

    def get_cache_key(self, ident, scope, rate):
        return self.cache_format % { "scope": scope, "ident": ident, "rate": rate }

    def wait_time(self, history, rate, now):
        rate_num_requests = rate[1]
        rate_duration = rate[2]

        if history:
            remaining_duration = rate_duration - (now - history[-1])
        else:
            remaining_duration = rate_duration

        available_requests = rate_num_requests - len(history) + 1
        if available_requests <= 0:
            return remaining_duration

        return remaining_duration / float(available_requests)

    def wait(self):
        return self._wait
