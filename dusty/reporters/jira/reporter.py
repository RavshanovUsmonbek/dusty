#!/usr/bin/python3
# coding=utf-8
# pylint: disable=I0011,E0401,R0914,R0912,R0915

#   Copyright 2019 getcarrier.io
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""
    Reporter: jira
"""

import re
import traceback

from datetime import datetime

from ruamel.yaml.comments import CommentedSeq
from ruamel.yaml.comments import CommentedMap

from dusty.tools import log
from dusty.models.module import DependentModuleModel
from dusty.models.reporter import ReporterModel
from dusty.models.finding import DastFinding, SastFinding
from dusty.models.error import Error

from dusty.constants import SEVERITIES

from . import constants
from .legacy import JiraWrapper, prepare_jira_mapping, cut_jira_comment


class Reporter(DependentModuleModel, ReporterModel):
    """ Report findings from scanners """

    def __init__(self, context):
        """ Initialize reporter instance """
        super().__init__()
        self.context = context
        self.config = \
            self.context.config["reporters"][__name__.split(".")[-2]]
        #
        self.set_meta("config", self.config)

    def report(self):
        """ Report """
        if self.config.get("dynamic_jira", None):
            log.info("Using MultiJira reporting")
            self.report_multi()
        else:
            log.info("Using normal reporting")
            self.report_normal()

    def report_multi(self):
        """ Report """
        wrappers_config = dict()
        wrappers_config[None] = self.config
        for key, value in self.config.get("dynamic_jira").items():
            wrappers_config[re.compile(key)] = value
        #
        wrappers = dict()
        for wrapper_key, wrapper_config in wrappers_config.items():
            wrapper = JiraWrapper(
                wrapper_config.get("url"),
                wrapper_config.get("username"),
                wrapper_config.get("password"),
                wrapper_config.get("project"),
                wrapper_config.get("fields"),
            )
            #
            if not wrapper.valid:
                # Save default mapping to meta as a fallback
                default_mapping = constants.JIRA_SEVERITY_MAPPING
                default_mapping.update(self.config.get("custom_mapping", dict()))
                self.set_meta("mapping", default_mapping)
                # Report error
                log.error("Jira configuration is invalid. Skipping Jira reporting")
                raise RuntimeError("Jira configuration is invalid")
            #
            wrappers[wrapper_key] = dict()
            wrappers[wrapper_key]["wrapper"] = wrapper
            wrappers[wrapper_key]["config"] = wrapper_config
            #
            if wrapper_config.get("separate_epic_linkage", False) and \
                    "Epic Link" in wrapper_config.get("fields"):
                wrappers[wrapper_key]["epic_link"] = wrapper_config.get("fields").pop("Epic Link")
            #
            wrappers[wrapper_key]["raw_epic_link"] = None
            if wrapper_config.get("separate_epic_linkage", False):
                wrappers[wrapper_key]["raw_epic_link"] = wrappers[wrapper_key]["epic_link"]
            elif "Epic Link" in wrapper_config.get("fields"):
                wrappers[wrapper_key]["raw_epic_link"] = wrapper_config.get("fields")["Epic Link"]
            #
            wrappers[wrapper_key]["priority_mapping"] = wrapper_config.get(
                "custom_mapping", prepare_jira_mapping(wrapper)
            )
            wrappers[wrapper_key]["mapping_meta"] = dict(wrappers[wrapper_key]["priority_mapping"])
            #
        self.set_meta("wrapper", wrappers[None]["wrapper"])
        self.set_meta("raw_epic_link", wrappers[None]["raw_epic_link"])
        #
        dynamic_label_mapping = dict()
        if self.config.get("dynamic_labels", None):
            try:
                for key, value in self.config.get("dynamic_labels").items():
                    dynamic_label_mapping[re.compile(key)] = value
            except:  # pylint: disable=W0702
                log.exception("Failed to add dynamic label mapping")
        #
        dynamic_field_mapping = dict()
        if self.config.get("dynamic_fields", None):
            try:
                for key, value in self.config.get("dynamic_fields").items():
                    dynamic_field_mapping[re.compile(key)] = value
            except:  # pylint: disable=W0702
                log.exception("Failed to add dynamic field mapping")
        #
        findings = list()
        for item in self.context.findings:  # pylint: disable=R1702
            #
            if item.get_meta("information_finding", False) or \
                    item.get_meta("false_positive_finding", False) or \
                    item.get_meta("excluded_finding", False):
                continue
            #
            if isinstance(item, (DastFinding, SastFinding)):
                #
                dynamic_labels = list()
                dynamic_fields = list()
                dynamic_wrapper = wrappers[None]
                #
                for endpoint in item.get_meta("endpoints", list()):
                    #
                    for pattern, addon_label in dynamic_label_mapping.items():
                        try:
                            if pattern.match(endpoint.raw):
                                dynamic_labels.append(addon_label)
                        except:  # pylint: disable=W0702
                            log.exception("Failed to add dynamic label")
                    #
                    for pattern, addon_fields in dynamic_field_mapping.items():
                        try:
                            if pattern.match(endpoint.raw):
                                dynamic_fields.append(addon_fields)
                        except:  # pylint: disable=W0702
                            log.exception("Failed to add dynamic field")
                    #
                    for pattern, addon_jira in wrappers.items():
                        if pattern is None:
                            continue
                        try:
                            if pattern.match(endpoint.raw):
                                dynamic_wrapper = addon_jira
                        except:  # pylint: disable=W0702
                            log.exception("Failed to add dynamic JIRA")
                #
                severity = item.get_meta("severity", SEVERITIES[-1])
                priority = constants.JIRA_SEVERITY_MAPPING[severity]
                if dynamic_wrapper["priority_mapping"] and \
                        priority in dynamic_wrapper["priority_mapping"]:
                    priority = dynamic_wrapper["priority_mapping"][priority]
                dynamic_wrapper["mapping_meta"][severity] = priority
                #
                if isinstance(item, DastFinding):
                    findings.append({
                        "title": item.title,
                        "priority": priority,
                        "description": item.description.replace("\\.", "."),
                        "issue_hash": item.get_meta("issue_hash", "<no_hash>"),
                        "additional_labels": [
                            label.replace(" ", "_") for label in [
                                item.get_meta("tool", "scanner"),
                                self.context.get_meta("testing_type", "DAST"),
                                item.get_meta("severity", SEVERITIES[-1])
                            ]
                        ] + dynamic_labels,
                        "dynamic_fields": dynamic_fields,
                        "raw": item,
                        "wrapper": dynamic_wrapper,
                    })
                elif isinstance(item, SastFinding):
                    #
                    description_chunks = [
                        item.replace(
                            "\\.", "."
                        ).replace(
                            "<pre>", "{code:collapse=true}\n\n"
                        ).replace(
                            "</pre>", "\n\n{code}"
                        ).replace(
                            "<br />", "\n"
                        ) for item in item.description
                    ]
                    #
                    if len("\n\n".join(description_chunks)) > constants.JIRA_DESCRIPTION_MAX_SIZE:
                        description = description_chunks[0]
                        chunks = description_chunks[1:]
                        comments = list()
                        new_line_str = '  \n  \n'
                        for chunk in chunks:
                            if not comments or (
                                    len(comments[-1]) + len(new_line_str) + len(chunk)
                                ) >= \
                                    constants.JIRA_COMMENT_MAX_SIZE:
                                comments.append(cut_jira_comment(chunk))
                            else:  # Last comment can handle one more chunk
                                comments[-1] += new_line_str + cut_jira_comment(chunk)
                    else:
                        description = "\n\n".join(description_chunks)
                        comments = list()
                    #
                    findings.append({
                        "title": item.title,
                        "priority": priority,
                        "description": description,
                        "issue_hash": item.get_meta("issue_hash", "<no_hash>"),
                        "additional_labels": [
                            label.replace(" ", "_") for label in [
                                item.get_meta("tool", "scanner"),
                                self.context.get_meta("testing_type", "SAST"),
                                item.get_meta("severity", SEVERITIES[-1])
                            ]
                        ] + dynamic_labels,
                        "dynamic_fields": dynamic_fields,
                        "comments": comments,
                        "raw": item,
                        "wrapper": dynamic_wrapper,
                    })
                #
            #
            else:
                log.warning("Unsupported finding type")
                continue # raise ValueError("Unsupported item type")
        #
        self.set_meta("mapping", wrappers[None]["mapping_meta"])
        #
        for finding in findings:
            if finding["wrapper"]["config"].get("max_description_size", False):
                if len(finding["description"]) > \
                        int(finding["wrapper"]["config"].get("max_description_size")):
                    if "comments" not in finding:
                        finding["comments"] = list()
                    #
                    comment_chunks = list()
                    cut_line_len = len(constants.JIRA_DESCRIPTION_CUT)
                    cut_point = int(
                        finding["wrapper"]["config"].get("max_description_size")
                    ) - cut_line_len
                    #
                    item_description = finding["description"]
                    finding["description"] = \
                        f"{item_description[:cut_point]}{constants.JIRA_DESCRIPTION_CUT}"
                    #
                    description_data = item_description[cut_point:]
                    comment_cut_threshold = min(
                        constants.JIRA_COMMENT_MAX_SIZE,
                        int(finding["wrapper"]["config"].get("max_description_size"))
                    )
                    cut_point = comment_cut_threshold - cut_line_len
                    #
                    while description_data:
                        if len(description_data) > comment_cut_threshold:
                            comment_chunks.append(
                                f"{description_data[:cut_point]}{constants.JIRA_DESCRIPTION_CUT}"
                            )
                            description_data = description_data[cut_point:]
                        else:
                            comment_chunks.append(description_data)
                            break
                    #
                    while comment_chunks:
                        finding["comments"].insert(0, comment_chunks.pop())
        #
        findings.sort(key=lambda item: (
            SEVERITIES.index(item["raw"].get_meta("severity", SEVERITIES[-1])),
            item["raw"].get_meta("tool", ""),
            item["raw"].title
        ))
        #
        new_tickets = list()
        existing_tickets = list()
        #
        for _, local_wrapper in wrappers.items():
            local_wrapper["wrapper"].connect()
        #
        for finding in findings:
            try:
                config_labels = finding["wrapper"]["config"].get("additional_labels", None)
                if config_labels is None:
                    config_labels = list()
                if not isinstance(config_labels, list):
                    config_labels = [item.strip() for item in config_labels.split(",")]
                #
                field_overrides = dict()
                for dynamic_field in finding["dynamic_fields"]:
                    field_overrides.update(dynamic_field)
                #
                issue, created = finding["wrapper"]["wrapper"].create_issue(
                    finding["title"], # title
                    finding["priority"], # priority
                    finding["description"], # description
                    finding["issue_hash"], # issue_hash, self.get_hash_code()
                    # attachments=None,
                    # get_or_create=True,
                    additional_labels=finding["additional_labels"]+config_labels, # additional_labels  # pylint: disable=C0301
                    field_overrides=field_overrides,
                )
                if created and "comments" in finding:
                    for comment in finding["comments"]:
                        finding["wrapper"]["wrapper"].add_comment_to_issue(issue, comment)
                if created and finding["wrapper"]["config"].get("separate_epic_linkage", False):
                    try:
                        finding["wrapper"]["wrapper"].client.add_issues_to_epic(
                            finding["wrapper"]["epic_link"], [str(issue.key)]
                        )
                    except:  # pylint: disable=W0702
                        log.exception(
                            "Failed to add ticket %s to epic %s", str(issue.key),
                            finding["wrapper"]["epic_link"]
                        )
                try:
                    result_priority = str(issue.fields.priority)
                except:  # pylint: disable=W0702
                    result_priority = "Default"
                #
                ticket_meta = {
                    "jira_id": issue.key,
                    "jira_url": f"{finding['wrapper']['config'].get('url')}/browse/{issue.key}",  # pylint: disable=C0301
                    "priority": result_priority,
                    "status": issue.fields.status.name,
                    "created": issue.fields.created,
                    "open_date": datetime.strptime(
                        issue.fields.created, "%Y-%m-%dT%H:%M:%S.%f%z").strftime("%d %b %Y %H:%M"),
                    "description": issue.fields.summary,
                    "assignee": str(issue.fields.assignee),
                    "raw_created": str(issue.fields.created),
                    "raw_severity": finding["raw"].get_meta("severity", SEVERITIES[-1]),
                    "raw_jira_url": finding["wrapper"]["config"].get("url"),
                    "raw_jira_project": finding["wrapper"]["config"].get("project"),
                    "raw_jira_epic": finding["wrapper"]["raw_epic_link"],
                    "raw_jira_fields": finding["wrapper"]["config"].get("fields"),
                    "raw_addon_fields": field_overrides,
                    "raw_addon_labels": finding["additional_labels"]+config_labels,
                }
                if created:
                    if not self._ticket_in_list(ticket_meta, new_tickets):
                        new_tickets.append(ticket_meta)
                else:
                    if issue.fields.status.name in constants.JIRA_OPENED_STATUSES:
                        if not self._ticket_in_list(ticket_meta, existing_tickets):
                            existing_tickets.append(ticket_meta)
            except:  # pylint: disable=W0702
                log.exception(f"Failed to create ticket for {finding['title']}")
                error = Error(
                    tool=self.get_name(),
                    error=f"Failed to create ticket for {finding['title']}",
                    details=f"```\n{traceback.format_exc()}\n```"
                )
                self.errors.append(error)
        #
        self.set_meta("new_tickets", new_tickets)
        self.set_meta("existing_tickets", existing_tickets)

    def report_normal(self):
        """ Report """
        # Remove "Epic Link" from fields if requested
        if self.config.get("separate_epic_linkage", False) and \
                "Epic Link" in self.config.get("fields"):
            epic_link = self.config.get("fields").pop("Epic Link")
        # Save raw Epic Link
        raw_epic_link = None
        if self.config.get("separate_epic_linkage", False):
            raw_epic_link = epic_link
        elif "Epic Link" in self.config.get("fields"):
            raw_epic_link = self.config.get("fields")["Epic Link"]
        self.set_meta("raw_epic_link", raw_epic_link)
        # Prepare wrapper
        log.info("Creating legacy wrapper instance")
        wrapper = JiraWrapper(
            self.config.get("url"),
            self.config.get("username"),
            self.config.get("password"),
            self.config.get("project"),
            self.config.get("fields")
        )
        if not wrapper.valid:
            # Save default mapping to meta as a fallback
            default_mapping = constants.JIRA_SEVERITY_MAPPING
            default_mapping.update(self.config.get("custom_mapping", dict()))
            self.set_meta("mapping", default_mapping)
            # Report error
            log.error("Jira configuration is invalid. Skipping Jira reporting")
            raise RuntimeError("Jira configuration is invalid")
        log.debug("Legacy wrapper is valid")
        self.set_meta("wrapper", wrapper)
        # Prepare dynamic label mapping
        dynamic_label_mapping = dict()
        if self.config.get("dynamic_labels", None):
            try:
                for key, value in self.config.get("dynamic_labels").items():
                    dynamic_label_mapping[re.compile(key)] = value
            except:  # pylint: disable=W0702
                log.exception("Failed to add dynamic label mapping")
        # Prepare dynamic fields mapping
        dynamic_field_mapping = dict()
        if self.config.get("dynamic_fields", None):
            try:
                for key, value in self.config.get("dynamic_fields").items():
                    dynamic_field_mapping[re.compile(key)] = value
            except:  # pylint: disable=W0702
                log.exception("Failed to add dynamic field mapping")
        # Prepare findings
        priority_mapping = self.config.get("custom_mapping", prepare_jira_mapping(wrapper))
        mapping_meta = dict(priority_mapping)
        findings = list()
        for item in self.context.findings:  # pylint: disable=R1702
            if item.get_meta("information_finding", False) or \
                    item.get_meta("false_positive_finding", False) or \
                    item.get_meta("excluded_finding", False):
                continue
            #
            if isinstance(item, DastFinding):
                severity = item.get_meta("severity", SEVERITIES[-1])
                priority = constants.JIRA_SEVERITY_MAPPING[severity]
                if priority_mapping and priority in priority_mapping:
                    priority = priority_mapping[priority]
                mapping_meta[severity] = priority  # Update meta mapping to reflect actual results
                #
                dynamic_labels = list()
                dynamic_fields = list()
                #
                for endpoint in item.get_meta("endpoints", list()):
                    #
                    for pattern, addon_label in dynamic_label_mapping.items():
                        try:
                            if pattern.match(endpoint.raw):
                                dynamic_labels.append(addon_label)
                        except:  # pylint: disable=W0702
                            log.exception("Failed to add dynamic label")
                    #
                    for pattern, addon_fields in dynamic_field_mapping.items():
                        try:
                            if pattern.match(endpoint.raw):
                                dynamic_fields.append(addon_fields)
                        except:  # pylint: disable=W0702
                            log.exception("Failed to add dynamic field")
                #
                findings.append({
                    "title": item.title,
                    "priority": priority,
                    "description": item.description.replace("\\.", "."),
                    "issue_hash": item.get_meta("issue_hash", "<no_hash>"),
                    "additional_labels": [
                        label.replace(" ", "_") for label in [
                            item.get_meta("tool", "scanner"),
                            self.context.get_meta("testing_type", "DAST"),
                            item.get_meta("severity", SEVERITIES[-1])
                        ]
                    ] + dynamic_labels,
                    "dynamic_fields": dynamic_fields,
                    "raw": item
                })
            #
            elif isinstance(item, SastFinding):
                severity = item.get_meta("severity", SEVERITIES[-1])
                priority = constants.JIRA_SEVERITY_MAPPING[severity]
                if priority_mapping and priority in priority_mapping:
                    priority = priority_mapping[priority]
                mapping_meta[severity] = priority  # Update meta mapping to reflect actual results
                description_chunks = [
                    item.replace(
                        "\\.", "."
                    ).replace(
                        "<pre>", "{code:collapse=true}\n\n"
                    ).replace(
                        "</pre>", "\n\n{code}"
                    ).replace(
                        "<br />", "\n"
                    ) for item in item.description
                ]
                if len("\n\n".join(description_chunks)) > constants.JIRA_DESCRIPTION_MAX_SIZE:
                    description = description_chunks[0]
                    chunks = description_chunks[1:]
                    comments = list()
                    new_line_str = '  \n  \n'
                    for chunk in chunks:
                        if not comments or (len(comments[-1]) + len(new_line_str) + len(chunk)) >= \
                                constants.JIRA_COMMENT_MAX_SIZE:
                            comments.append(cut_jira_comment(chunk))
                        else:  # Last comment can handle one more chunk
                            comments[-1] += new_line_str + cut_jira_comment(chunk)
                else:
                    description = "\n\n".join(description_chunks)
                    comments = list()
                #
                dynamic_labels = list()
                dynamic_fields = list()
                #
                for endpoint in item.get_meta("endpoints", list()):
                    #
                    for pattern, addon_label in dynamic_label_mapping.items():
                        try:
                            if pattern.match(endpoint.raw):
                                dynamic_labels.append(addon_label)
                        except:  # pylint: disable=W0702
                            log.exception("Failed to add dynamic label")
                    #
                    for pattern, addon_fields in dynamic_field_mapping.items():
                        try:
                            if pattern.match(endpoint.raw):
                                dynamic_fields.append(addon_fields)
                        except:  # pylint: disable=W0702
                            log.exception("Failed to add dynamic field")
                #
                findings.append({
                    "title": item.title,
                    "priority": priority,
                    "description": description,
                    "issue_hash": item.get_meta("issue_hash", "<no_hash>"),
                    "additional_labels": [
                        label.replace(" ", "_") for label in [
                            item.get_meta("tool", "scanner"),
                            self.context.get_meta("testing_type", "SAST"),
                            item.get_meta("severity", SEVERITIES[-1])
                        ]
                    ] + dynamic_labels,
                    "dynamic_fields": dynamic_fields,
                    "comments": comments,
                    "raw": item
                })
            #
            else:
                log.warning("Unsupported finding type")
                continue # raise ValueError("Unsupported item type")
        # Cut description if length above configured limit
        if self.config.get("max_description_size", False):
            for finding in findings:
                if len(finding["description"]) > int(self.config.get("max_description_size")):
                    if "comments" not in finding:
                        finding["comments"] = list()
                    #
                    comment_chunks = list()
                    cut_line_len = len(constants.JIRA_DESCRIPTION_CUT)
                    cut_point = int(self.config.get("max_description_size")) - cut_line_len
                    #
                    item_description = finding["description"]
                    finding["description"] = \
                        f"{item_description[:cut_point]}{constants.JIRA_DESCRIPTION_CUT}"
                    #
                    description_data = item_description[cut_point:]
                    comment_cut_threshold = min(
                        constants.JIRA_COMMENT_MAX_SIZE,
                        int(self.config.get("max_description_size"))
                    )
                    cut_point = comment_cut_threshold - cut_line_len
                    #
                    while description_data:
                        if len(description_data) > comment_cut_threshold:
                            comment_chunks.append(
                                f"{description_data[:cut_point]}{constants.JIRA_DESCRIPTION_CUT}"
                            )
                            description_data = description_data[cut_point:]
                        else:
                            comment_chunks.append(description_data)
                            break
                    #
                    while comment_chunks:
                        finding["comments"].insert(0, comment_chunks.pop())
        # Sort findings by severity-tool-title
        findings.sort(key=lambda item: (
            SEVERITIES.index(item["raw"].get_meta("severity", SEVERITIES[-1])),
            item["raw"].get_meta("tool", ""),
            item["raw"].title
        ))
        # Submit issues
        wrapper.connect()
        new_tickets = list()
        existing_tickets = list()
        for finding in findings:
            try:
                #
                config_labels = self.config.get("additional_labels", None)
                if config_labels is None:
                    config_labels = list()
                if not isinstance(config_labels, list):
                    config_labels = [item.strip() for item in config_labels.split(",")]
                #
                field_overrides = dict()
                for dynamic_field in finding["dynamic_fields"]:
                    field_overrides.update(dynamic_field)
                #
                issue, created = wrapper.create_issue(
                    finding["title"], # title
                    finding["priority"], # priority
                    finding["description"], # description
                    finding["issue_hash"], # issue_hash, self.get_hash_code()
                    # attachments=None,
                    # get_or_create=True,
                    additional_labels=finding["additional_labels"]+config_labels, # additional_labels  # pylint: disable=C0301
                    field_overrides=field_overrides,
                )
                if created and "comments" in finding:
                    for comment in finding["comments"]:
                        wrapper.add_comment_to_issue(issue, comment)
                if created and self.config.get("separate_epic_linkage", False):
                    try:
                        wrapper.client.add_issues_to_epic(epic_link, [str(issue.key)])
                    except:  # pylint: disable=W0702
                        log.exception(
                            "Failed to add ticket %s to epic %s", str(issue.key), epic_link
                        )
                try:
                    result_priority = str(issue.fields.priority)
                except:  # pylint: disable=W0702
                    result_priority = "Default"
                #
                ticket_meta = {
                    "jira_id": issue.key,
                    "jira_url": f"{self.config.get('url')}/browse/{issue.key}",  # pylint: disable=C0301
                    "priority": result_priority,
                    "status": issue.fields.status.name,
                    "created": issue.fields.created,
                    "open_date": datetime.strptime(
                        issue.fields.created, "%Y-%m-%dT%H:%M:%S.%f%z").strftime("%d %b %Y %H:%M"),
                    "description": issue.fields.summary,
                    "assignee": str(issue.fields.assignee),
                    "raw_created": str(issue.fields.created),
                    "raw_severity": finding["raw"].get_meta("severity", SEVERITIES[-1]),
                    "raw_jira_url": self.config.get("url"),
                    "raw_jira_project": self.config.get("project"),
                    "raw_jira_epic": raw_epic_link,
                    "raw_jira_fields": self.config.get("fields"),
                    "raw_addon_fields": field_overrides,
                    "raw_addon_labels": finding["additional_labels"]+config_labels,
                }
                if created:
                    if not self._ticket_in_list(ticket_meta, new_tickets):
                        new_tickets.append(ticket_meta)
                else:
                    if issue.fields.status.name in constants.JIRA_OPENED_STATUSES:
                        if not self._ticket_in_list(ticket_meta, existing_tickets):
                            existing_tickets.append(ticket_meta)
            except:  # pylint: disable=W0702
                log.exception(f"Failed to create ticket for {finding['title']}")
                error = Error(
                    tool=self.get_name(),
                    error=f"Failed to create ticket for {finding['title']}",
                    details=f"```\n{traceback.format_exc()}\n```"
                )
                self.errors.append(error)
        self.set_meta("new_tickets", new_tickets)
        self.set_meta("existing_tickets", existing_tickets)
        self.set_meta("mapping", mapping_meta)

    @staticmethod
    def _ticket_in_list(ticket_meta, tickets_list):
        for item in tickets_list:
            if item["jira_id"] == ticket_meta["jira_id"]:
                return True
        return False

    @staticmethod
    def fill_config(data_obj):
        """ Make sample config """
        data_obj.insert(len(data_obj), "url", "https://jira.example.com", comment="Jira URL")
        data_obj.insert(
            len(data_obj), "username", "some_username", comment="Jira login"
        )
        data_obj.insert(
            len(data_obj), "password", "SomeSecurePassword", comment="Jira password"
        )
        data_obj.insert(
            len(data_obj), "project", "SOME-PROJECT", comment="Jira project"
        )
        data_obj.insert(
            len(data_obj), "fields", CommentedMap(), comment="Fields for created tickets"
        )
        fields_obj = data_obj["fields"]
        fields_obj.insert(
            len(fields_obj),
            "Issue Type", "Bug", comment="(field) Ticket type"
        )
        fields_obj.insert(
            len(fields_obj),
            "Assignee", "Ticket_Assignee", comment="(field) Assignee"
        )
        fields_obj.insert(
            len(fields_obj),
            "Epic Link", "SOMEPROJECT-1234", comment="(field) Epic"
        )
        fields_obj.insert(
            len(fields_obj),
            "Security Level", "SOME_LEVEL", comment="(field) Security level"
        )
        fields_obj.insert(
            len(fields_obj),
            "Components/s", CommentedSeq(), comment="(field) Component/s"
        )
        components_obj = fields_obj["Components/s"]
        component_obj = CommentedMap()
        component_obj.insert(len(component_obj), "name", "Component Name")
        components_obj.append(component_obj)
        data_obj.insert(
            len(data_obj), "custom_mapping", CommentedMap(), comment="Custom priority mapping"
        )
        mapping_obj = data_obj["custom_mapping"]
        mapping_obj.insert(
            len(mapping_obj),
            "Critical", "Very High"
        )
        mapping_obj.insert(
            len(mapping_obj),
            "Major", "High"
        )
        mapping_obj.insert(
            len(mapping_obj),
            "Medium", "Medium"
        )
        mapping_obj.insert(
            len(mapping_obj),
            "Minor", "Low"
        )
        mapping_obj.insert(
            len(mapping_obj),
            "Trivial", "Low"
        )
        data_obj.insert(
            len(data_obj), "separate_epic_linkage", False,
            comment="(optional) Link to Epics after ticket creation"
        )
        data_obj.insert(
            len(data_obj), "max_description_size", constants.JIRA_DESCRIPTION_MAX_SIZE,
            comment="(optional) Cut description longer than set limit"
        )

    @staticmethod
    def validate_config(config):
        """ Validate config """
        required = ["url", "username", "password", "project"]
        not_set = [item for item in required if item not in config]
        if not_set:
            error = f"Required configuration options not set: {', '.join(not_set)}"
            log.error(error)
            raise ValueError(error)

    @staticmethod
    def get_name():
        """ Reporter name """
        return "Jira"

    @staticmethod
    def get_description():
        """ Reporter description """
        return "Jira reporter"
