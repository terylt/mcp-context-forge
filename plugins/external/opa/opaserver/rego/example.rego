
# Package for sample rego policies
# Copyright 2025
# SPDX-License-Identifier: Apache-2.0
# Authors: Shriti Priya
# This file is responsible for rego policies for each type of requests made, it could be prompt, resource or tool requests

package example



# Default policy values for all the policies
default allow_pre_tool := false
default allow_post_tool := false
default allow_pre_prompt := false
default allow_post_prompt := false
default allow_pre_resource := false
default allow_post_resource := false


# Policies applied for pre tool invocations
allow_pre_tool if {
    contains(input.tool.args.repo_path, "IBM")
}

# Policies applied for post tool invocations
allow_post_tool if {
    contains(input.tool.args.repo_path, "IBM")
}

# Policies applied for pre prompt invocations
allow_pre_prompt if {
    input.prompt.args.text == "allowed-word"
}

# Policies applied for post prompt invocations
allow_post_path if {
    input.prompt.args.text == "allowed-word"
}

# Policies applied for pre resource invocations
allow_pre_resource if {
    input.uri == "allowed-domain"
}

# Policies applied for post resource invocations
allow_post_resource if {
    input.uri == "allowed-domain"
}