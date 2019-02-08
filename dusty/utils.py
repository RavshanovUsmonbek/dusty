#   Copyright 2018 getcarrier.io
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

import re
import os
from subprocess import Popen, PIPE

from dusty import constants


def report_to_rp(config, result, issue_name):
    if config.get("rp_config"):
        rp_data_writer = config['rp_data_writer']
        rp_data_writer.start_test_item(issue=issue_name, tags=[], description=f"Results of {issue_name} scan",
                                       item_type="SUITE")
        for item in result:
            item.rp_item(rp_data_writer)
        rp_data_writer.finish_test_item()


def report_to_jira(config, result):
    if config.get('jira_service') and config.get('jira_service').valid:
        config.get('jira_service').connect()
        print(config.get('jira_service').client)
        for item in result:
            issue, created = item.jira(config['jira_service'])
            if created:
                print(issue.key)
    elif config.get('jira_service') and not config.get('jira_service').valid:
        print("Jira Configuration incorrect, please fix ... ")


def send_emails(emails_service, jira_is_used, jira_tickets_info, attachments):
    if emails_service and emails_service.valid:
        if jira_is_used:
            if jira_tickets_info:
                html = """\
                        <p>Here’s the list of security issues found: </p>
                        <table>
                            <tr>
                                <th>PRIORITY</th>
                                <th>KEY</th>
                                <th>SUMMARY</th>
                            </tr>
                            {}
                        </table>
                    """
                table_rows = '\n'.join(['<tr><td>{}</td><td><a href="{}">{}</a></td><td>{}</td></tr>'.format(
                    x['priority'], x['link'], x['key'], x['summary']) for x in jira_tickets_info])
                html_body = html.format(table_rows)
            else:
                html_body = '<p>No new security issues bugs found.</p>'
        else:
            html_body = '<p>Please see the results attached.</p>'
        html_style = """
                    table, th, td {
                      border: 1px solid black;
                      border-collapse: collapse;
                      padding: 0px 5px;
                    }
                """
        emails_service.send(html_body=html_body, html_style=html_style, attachments=attachments)
    elif emails_service and not emails_service.valid:
        print("Email Configuration incorrect, please fix ... ")


def execute(exec_cmd, cwd='/tmp', communicate=True):
    print(f'Running: {exec_cmd}')
    proc = Popen(exec_cmd.split(" "), cwd=cwd, stdout=PIPE, stderr=PIPE)
    if communicate:
        res = proc.communicate()
        print("Done")
        if os.environ.get("debug", False):
            print(f"stdout: {res[0]}")
            print(f"stderr: {res[1]}")
        return res
    else:
        return proc


def find_ip(str):
    ip_pattern = re.compile('\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\s')
    ip = re.findall(ip_pattern, str)
    return ip


def process_false_positives(results):
    false_positives = []
    if os.path.exists(constants.FALSE_POSITIVE_CONFIG):
        with open(constants.FALSE_POSITIVE_CONFIG, 'r') as f:
            for line in f.readlines():
                if line.strip():
                    false_positives.append(line.strip())
    if not false_positives:
        return results
    to_remove = []
    results = list(results)
    for index in range(len(results)):
        if results[index].get_hash_code() in false_positives:
            to_remove.append(results[index])
    for _ in to_remove:
        results.pop(results.index(_))
    return results


def common_post_processing(config, result, tool_name):
    result = process_false_positives(result)
    report_to_rp(config, result, tool_name)
    report_to_jira(config, result)


def ptai_post_processing(config, result):
    result = process_false_positives(result)
    report_to_jira(config, result)
