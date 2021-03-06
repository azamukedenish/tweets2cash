# -*- coding: utf-8 -*-

"""
This module contains a domain logic for authentication
process. It called services because in DDD says it.

NOTE: Python doesn't have java limitations for "everytghing
should be contained in a class". Because of that, it
not uses clasess and uses simple functions.
"""

from django.contrib.auth import get_user_model
from django.db import transaction as tx
from django.db import IntegrityError
from django.utils.translation import ugettext as _

from rest_framework import exceptions as exc
from tweets2cash.base.mails import mail_builder
from tweets2cash.users.serializers import UserAdminSerializer
from tweets2cash.users.services import get_and_validate_user

from .tokens import get_token_for_user
from .signals import user_registered as user_registered_signal
from django.apps import apps
import string
import random
import uuid
from tweets2cash.users import models

auth_plugins = {}

def register_auth_plugin(name, login_func):
    auth_plugins[name] = {
        "login_func": login_func,
    }


def get_auth_plugins():
    return auth_plugins


def send_register_email(user):
    """
    Given a user, send register welcome email
    message to specified user.
    """
    # We need to generate a token for the email
    cancel_token = get_token_for_user(user, "cancel_account")
    context = {"user": user, "cancel_token": cancel_token,}
    email = mail_builder.registered_user(user, context)
    return bool(email.send())


def is_user_already_registered(username, email):
    """
    Checks if a specified user is already registred.

    Returns a tuple containing a boolean value that indicates if the user exists
    and in case he does whats the duplicated attribute
    """

    user_model = get_user_model()
    if user_model.objects.filter(username__iexact=username):
        return (True, _("Username is already in use."))

    if user_model.objects.filter(email__iexact=email):
        return (True, _("Email is already in use."))

    return (False, None)


@tx.atomic
def public_register(username, password, email, full_name):
    """
    Given a parsed parameters, try register a new user
    knowing that it follows a public register flow.

    This can raise `exc.IntegrityError` exceptions in
    case of conflics found.

    :returns: User
    """

    is_registered, reason = is_user_already_registered(username=username, email=email)
    if is_registered:
        raise exc.WrongArguments(reason)

    user_model = get_user_model()
    user = user_model(username=username,
                      email=email,
                      full_name=full_name)
    user.set_password(password)
    try:
        user.save()
    except IntegrityError:
        raise exc.WrongArguments(_("User is already registered."))

    send_register_email(user)
    user_registered_signal.send(sender=user.__class__, user=user)
    return user


def make_auth_response_data(user):
    """
    Given a domain and user, creates data structure
    using python dict containing a representation
    of the logged user.
    """
    serializer = UserAdminSerializer(user)
    data = dict(serializer.data)
    data["auth_token"] = get_token_for_user(user, "authentication")
    return data


def normal_login_func(request):
    username = request.data.get('username', None)
    password = request.data.get('password', None)

    user = get_and_validate_user(username=username, password=password)
    data = make_auth_response_data(user)
    return data


register_auth_plugin("normal", normal_login_func)
