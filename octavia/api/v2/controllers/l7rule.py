#    Copyright 2016 Blue Box, an IBM Company
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging

from oslo_db import exception as odb_exceptions
from oslo_utils import excutils
import pecan
from wsme import types as wtypes
from wsmeext import pecan as wsme_pecan

from octavia.api.v2.controllers import base
from octavia.api.v2.types import l7rule as l7rule_types
from octavia.common import constants
from octavia.common import data_models
from octavia.common import exceptions
from octavia.common import validate
from octavia.db import api as db_api
from octavia.db import prepare as db_prepare


LOG = logging.getLogger(__name__)


class L7RuleController(base.BaseController):

    def __init__(self, l7policy_id):
        super(L7RuleController, self).__init__()
        self.l7policy_id = l7policy_id
        self.handler = self.handler.l7rule

    @wsme_pecan.wsexpose(l7rule_types.L7RuleRootResponse, wtypes.text)
    def get(self, id):
        """Gets a single l7rule's details."""
        context = pecan.request.context.get('octavia_context')
        db_l7rule = self._get_db_l7rule(context.session, id)
        result = self._convert_db_to_type(db_l7rule,
                                          l7rule_types.L7RuleResponse)
        return l7rule_types.L7RuleRootResponse(rule=result)

    @wsme_pecan.wsexpose(l7rule_types.L7RulesRootResponse, wtypes.text,
                         ignore_extra_args=True)
    def get_all(self):
        """Lists all l7rules of a l7policy."""
        pcontext = pecan.request.context
        context = pcontext.get('octavia_context')
        db_l7rules, links = self.repositories.l7rule.get_all(
            context.session, show_deleted=False, l7policy_id=self.l7policy_id,
            pagination_helper=pcontext.get(constants.PAGINATION_HELPER))
        result = self._convert_db_to_type(
            db_l7rules, [l7rule_types.L7RuleResponse])
        return l7rule_types.L7RulesRootResponse(
            rules=result, rules_links=links)

    def _test_lb_listener_policy_statuses(self, session):
        """Verify load balancer is in a mutable state."""
        l7policy = self._get_db_l7policy(session, self.l7policy_id)
        listener_id = l7policy.listener_id
        load_balancer_id = l7policy.listener.load_balancer_id
        if not self.repositories.test_and_set_lb_and_listeners_prov_status(
                session, load_balancer_id,
                constants.PENDING_UPDATE, constants.PENDING_UPDATE,
                listener_ids=[listener_id], l7policy_id=self.l7policy_id):
            LOG.info("L7Rule cannot be created or modified because the "
                     "Load Balancer is in an immutable state")
            raise exceptions.ImmutableObject(resource='Load Balancer',
                                             id=load_balancer_id)

    def _check_l7policy_max_rules(self, session):
        """Checks to make sure the L7Policy doesn't have too many rules."""
        count = self.repositories.l7rule.count(
            session, l7policy_id=self.l7policy_id)
        if count >= constants.MAX_L7RULES_PER_L7POLICY:
            raise exceptions.TooManyL7RulesOnL7Policy(id=self.l7policy_id)

    def _reset_lb_listener_policy_statuses(self, session):
        # Setting LB + listeners back to active because this should be a
        # recoverable error
        l7policy = self._get_db_l7policy(session, self.l7policy_id)
        listener_id = l7policy.listener_id
        load_balancer_id = l7policy.listener.load_balancer_id
        self.repositories.load_balancer.update(
            session, load_balancer_id,
            provisioning_status=constants.ACTIVE)
        self.repositories.listener.update(
            session, listener_id,
            provisioning_status=constants.ACTIVE)
        self.repositories.l7policy.update(
            session, self.l7policy_id,
            provisioning_status=constants.ACTIVE)

    def _validate_create_l7rule(self, lock_session, l7rule_dict):
        try:
            return self.repositories.l7rule.create(lock_session, **l7rule_dict)
        except odb_exceptions.DBDuplicateEntry as de:
            if ['id'] == de.columns:
                raise exceptions.IDAlreadyExists()
        except odb_exceptions.DBError as de:
            # TODO(blogan): will have to do separate validation protocol
            # before creation or update since the exception messages
            # do not give any information as to what constraint failed
            raise exceptions.InvalidOption(value='', option='')

    def _send_l7rule_to_handler(self, session, db_l7rule):
        try:
            LOG.info("Sending Creation of L7Rule %s to handler", db_l7rule.id)
            self.handler.create(db_l7rule)
        except Exception:
            with excutils.save_and_reraise_exception(
                    reraise=False), db_api.get_lock_session() as lock_session:
                self._reset_lb_listener_policy_statuses(lock_session)
                # L7Rule now goes to ERROR
                self.repositories.l7rule.update(
                    lock_session, db_l7rule.id,
                    provisioning_status=constants.ERROR)
        db_l7rule = self._get_db_l7rule(session, db_l7rule.id)
        result = self._convert_db_to_type(db_l7rule,
                                          l7rule_types.L7RuleResponse)
        return l7rule_types.L7RuleRootResponse(rule=result)

    @wsme_pecan.wsexpose(l7rule_types.L7RuleRootResponse,
                         body=l7rule_types.L7RuleRootPOST, status_code=201)
    def post(self, rule_):
        """Creates a l7rule on an l7policy."""
        l7rule = rule_.rule
        try:
            validate.l7rule_data(l7rule)
        except Exception as e:
            raise exceptions.L7RuleValidation(error=e)
        context = pecan.request.context.get('octavia_context')
        l7rule.project_id = self._get_l7policy_project_id(context.session,
                                                          self.l7policy_id)
        self._check_l7policy_max_rules(context.session)

        lock_session = db_api.get_session(autocommit=False)
        l7rule_dict = db_prepare.create_l7rule(
            l7rule.to_dict(render_unsets=True), self.l7policy_id)
        try:
            self._test_lb_listener_policy_statuses(context.session)

            db_l7rule = self._validate_create_l7rule(lock_session, l7rule_dict)
            lock_session.commit()
        except Exception:
            with excutils.save_and_reraise_exception():
                lock_session.rollback()

        return self._send_l7rule_to_handler(context.session, db_l7rule)

    def _graph_create(self, lock_session, rule_dict):
        try:
            validate.l7rule_data(l7rule_types.L7RulePOST(**rule_dict))
        except Exception as e:
            raise exceptions.L7RuleValidation(error=e)
        rule_dict = db_prepare.create_l7rule(rule_dict, self.l7policy_id)
        db_rule = self._validate_create_l7rule(lock_session, rule_dict)

        return db_rule

    @wsme_pecan.wsexpose(l7rule_types.L7RuleRootResponse,
                         wtypes.text, body=l7rule_types.L7RuleRootPUT,
                         status_code=200)
    def put(self, id, l7rule_):
        """Updates a l7rule."""
        l7rule = l7rule_.rule
        context = pecan.request.context.get('octavia_context')
        db_l7rule = self._get_db_l7rule(context.session, id)
        new_l7rule = db_l7rule.to_dict()
        new_l7rule.update(l7rule.to_dict())
        new_l7rule = data_models.L7Rule.from_dict(new_l7rule)
        try:
            validate.l7rule_data(new_l7rule)
        except Exception as e:
            raise exceptions.L7RuleValidation(error=e)
        self._test_lb_listener_policy_statuses(context.session)

        self.repositories.l7rule.update(
            context.session, db_l7rule.id,
            provisioning_status=constants.PENDING_UPDATE)

        try:
            LOG.info("Sending Update of L7Rule %s to handler", id)
            self.handler.update(db_l7rule, l7rule)
        except Exception:
            with excutils.save_and_reraise_exception(
                    reraise=False), db_api.get_lock_session() as lock_session:
                self._reset_lb_listener_policy_statuses(lock_session)
                # L7Rule now goes to ERROR
                self.repositories.l7rule.update(
                    lock_session, db_l7rule.id,
                    provisioning_status=constants.ERROR)
        db_l7rule = self._get_db_l7rule(context.session, id)
        result = self._convert_db_to_type(db_l7rule,
                                          l7rule_types.L7RuleResponse)
        return l7rule_types.L7RuleRootResponse(rule=result)

    @wsme_pecan.wsexpose(None, wtypes.text, status_code=204)
    def delete(self, id):
        """Deletes a l7rule."""
        context = pecan.request.context.get('octavia_context')
        db_l7rule = self._get_db_l7rule(context.session, id)
        self._test_lb_listener_policy_statuses(context.session)

        self.repositories.l7rule.update(
            context.session, db_l7rule.id,
            provisioning_status=constants.PENDING_DELETE)

        try:
            LOG.info("Sending Deletion of L7Rule %s to handler", db_l7rule.id)
            self.handler.delete(db_l7rule)
        except Exception:
            with excutils.save_and_reraise_exception(
                    reraise=False), db_api.get_lock_session() as lock_session:
                self._reset_lb_listener_policy_statuses(lock_session)
                # L7Rule now goes to ERROR
                self.repositories.l7rule.update(
                    lock_session, db_l7rule.id,
                    provisioning_status=constants.ERROR)
        db_l7rule = self.repositories.l7rule.get(context.session, id=id)
        result = self._convert_db_to_type(db_l7rule,
                                          l7rule_types.L7RuleResponse)
        return l7rule_types.L7RuleRootResponse(rule=result)
