# Copyright 2016 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

# modified from customizations/configure/writer.py and configure.py in aws-cli

import os
import re
import shlex

class SectionNotFoundError(Exception):
    pass

def get_config_filename(session):
    return os.path.expanduser(session.get_config_variable('config_file'))

_WHITESPACE = ' \t'
def process_profile_name(profile_name):
    if any(c in _WHITESPACE for c in profile_name):
        profile_name = shlex.quote(profile_name)
    return profile_name

def write_values(session, profile_name, values, config_file_writer=None, existing_config_action=None):
    if not config_file_writer:
        config_file_writer = ConfigFileWriter()

    new_values = values.copy()

    # The access_key/secret_key are now *always* written to the shared
    # credentials file (~/.aws/credentials), see aws/aws-cli#847.
    # post-conditions: ~/.aws/credentials will have the updated credential
    # file values and new_values will have the cred vars removed.
    credential_file_values = {}
    if 'aws_access_key_id' in new_values:
        credential_file_values['aws_access_key_id'] = new_values.pop(
            'aws_access_key_id')
    if 'aws_secret_access_key' in new_values:
        credential_file_values['aws_secret_access_key'] = new_values.pop(
            'aws_secret_access_key')
    if credential_file_values:
        if profile_name is not None:
            credential_file_values['__section__'] = profile_name
        shared_credentials_filename = os.path.expanduser(
            session.get_config_variable('credentials_file'))
        config_file_writer.update_config(
            credential_file_values,
            shared_credentials_filename)

    config_filename = os.path.expanduser(
        session.get_config_variable('config_file'))

    section = 'profile {}'.format(process_profile_name(profile_name))
    new_values['__section__'] = section
    config_file_writer.update_config(new_values, config_filename, existing_config_action)


class ConfigFileWriter(object):
    SECTION_REGEX = re.compile(r'^\s*\[(?P<header>[^]]+)\]')
    OPTION_REGEX = re.compile(
        r'(?P<option>[^:=][^:=]*)'
        r'\s*(?P<vi>[:=])\s*'
        r'(?P<value>.*)$'
    )

    def update_config(self, new_values, config_filename, existing_config_action=None):
        """Update config file with new values.

        This method will update a section in a config file with
        new key value pairs.

        This method provides a few conveniences:

        * If the ``config_filename`` does not exist, it will
          be created.  Any parent directories will also be created
          if necessary.
        * If the section to update does not exist, it will be created.


        :type new_values: dict
        :param new_values: The values to update.  There is a special
            key ``__section__``, that specifies what section in the INI
            file to update.  If this key is not present, then the
            ``default`` section will be updated with the new values.

        :type config_filename: str
        :param config_filename: The config filename where values will be
            written.

        :type existing_config_action: str
        :param existing_config_action: One of "overwrite" (the default),
            "keep", or "discard". The latter will discard all existing
            config for the section.

        """
        if existing_config_action is None:
            existing_config_action = "overwrite"
        section_name = new_values.pop('__section__', 'default')
        if not os.path.isfile(config_filename):
            self._create_file(config_filename)
            self._write_new_section(section_name, new_values, config_filename)
            return
        with open(config_filename, 'r') as f:
            contents = f.readlines()
        # We can only update a single section at a time so we first need
        # to find the section in question
        try:
            self._update_section_contents(contents, section_name, new_values, existing_config_action)
            with open(config_filename, 'w') as f:
                f.write(''.join(contents))
        except SectionNotFoundError:
            self._write_new_section(section_name, new_values, config_filename)

    def _create_file(self, config_filename):
        # Create the file as well as the parent dir if needed.
        dirname = os.path.split(config_filename)[0]
        if not os.path.isdir(dirname):
            os.makedirs(dirname)
        with os.fdopen(os.open(config_filename,
                               os.O_WRONLY | os.O_CREAT, 0o600), 'w'):
            pass

    def _write_new_section(self, section_name, new_values, config_filename):
        with open(config_filename, 'a') as f:
            f.write('\n[%s]\n' % section_name)
            contents = []
            self._insert_new_values(line_number=0,
                                    contents=contents,
                                    new_values=new_values)
            f.write(''.join(contents))

    def _find_section_start(self, contents, section_name):
        for i in range(len(contents)):
            line = contents[i]
            if line.strip().startswith(('#', ';')):
                # This is a comment, so we can safely ignore this line.
                continue
            match = self.SECTION_REGEX.search(line)
            if match is not None and self._matches_section(match,
                                                           section_name):
                return i
        raise SectionNotFoundError(section_name)

    def _update_section_contents(self, contents, section_name, new_values, existing_config_action):
        # First, find the line where the section_name is defined.
        # This will be the value of i.
        new_values = new_values.copy()
        # ``contents`` is a list of file line contents.
        section_start_line_num = self._find_section_start(contents,
                                                          section_name)
        # If we get here, then we've found the section.  We now need
        # to figure out if we're updating a value or adding a new value.
        # There's 2 cases.  Either we're setting a normal scalar value
        # of, we're setting a nested value.
        last_matching_line = section_start_line_num
        j = last_matching_line + 1
        while j < len(contents):
            line = contents[j]
            if self.SECTION_REGEX.search(line) is not None:
                # We've hit a new section which means the config key is
                # not in the section.  We need to add it here.
                self._insert_new_values(line_number=last_matching_line,
                                        contents=contents,
                                        new_values=new_values)
                return
            match = self.OPTION_REGEX.search(line)
            if match is not None:
                last_matching_line = j
                key_name = match.group(1).strip()
                if key_name in new_values:
                    # We've found the line that defines the option name.
                    # if the value is not a dict, then we can write the line
                    # out now.
                    if existing_config_action != "keep":
                        option_value = new_values[key_name]
                        new_line = '%s = %s\n' % (key_name, option_value)
                        contents[j] = new_line
                    del new_values[key_name]
                elif existing_config_action == "discard":
                    del contents[j]
                    j -= 1
            j += 1

        if new_values:
            if not contents[-1].endswith('\n'):
                contents.append('\n')
            self._insert_new_values(line_number=last_matching_line + 1,
                                    contents=contents,
                                    new_values=new_values)

    def _insert_new_values(self, line_number, contents, new_values, indent=''):
        new_contents = []
        for key, value in list(new_values.items()):
            if isinstance(value, dict):
                subindent = indent + '    '
                new_contents.append('%s%s =\n' % (indent, key))
                for subkey, subval in list(value.items()):
                    new_contents.append('%s%s = %s\n' % (subindent, subkey,
                                                         subval))
            else:
                new_contents.append('%s%s = %s\n' % (indent, key, value))
            del new_values[key]
        contents.insert(line_number + 1, ''.join(new_contents))

    def _matches_section(self, match, section_name):
        parts = section_name.split(' ')
        unquoted_match = match.group(0) == '[%s]' % section_name
        if len(parts) > 1:
            quoted_match = match.group(0) == '[%s "%s"]' % (
                parts[0], ' '.join(parts[1:]))
            return unquoted_match or quoted_match
        return unquoted_match

if __name__ == "__main__":
    import sys
    import json
    file_name = sys.argv[1]
    section_name = sys.argv[2]
    data = json.loads(sys.argv[3])
    data["__section__"] = section_name
    cfw = ConfigFileWriter()
    cfw.update_config(data, file_name, existing_config_action=sys.argv[4])
