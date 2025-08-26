package srepolicies

default allow = false
allow if {
    all_allowed
}

# all_allowed if {
#     command_keys := [k | input[k]; startswith(k, "command:")]
#     all_pass := [true | k := command_keys[_]; is_command_allowed(input[k])]
#     count(all_pass) == count(command_keys)
# }

all_allowed if {
    cmds := input.tool.args.commands
    all_pass := [true | cmd := cmds[_]; is_command_allowed(cmd)]
    count(all_pass) == count(cmds)
}



is_command_allowed(cmd) if {
    allow_command(cmd)
}

denied_commands[cmd.full_command] if {
    cmd := input.tool.args.commands[_]
    not is_command_allowed(cmd)
}

allow_command(cmd) if {
    cmd.command in ["kubectl top", "kubectl annotate", "kubectl label", "docker tag", "env", "ps", "tail", "netstat", "hostname", "ping", "dig", "ip", "ifconfig", "ss", "df", "free", "ls", "whoami", "uptime", "echo", "grep"]
}

allow_command(cmd) if {
    cmd.command == "kubectl wait"
    cmd.timeout < 60
}

allow_command(cmd) if {
    cmd.command == "sleep"
    cmd.timeout < 30
}

allow_command(cmd) if {
    contains(cmd.full_command, " --dry-run ")
}

allow_command(cmd) if {
    cmd.command == "kubectl rollout"
    cmd.ops in ["restart", "status", "history", "resume"]
    not cmd.ops in ["undo", "pause"]
}


allow_command(cmd) if {
    cmd.command in ["kubectl get", "kubectl describe", "kubectl logs", "kubectl cluster-info", "kubectl api-resources", "kubectl expose"]
    not prohibit_sensitive(cmd.full_command)
}

allow_command(cmd) = false if {
    cmd.command in ["kubectl run", "kubectl exec"]
    contains(cmd.full_command, " -it ")
    contains(cmd.full_command, "/bin/sh ")
}

allow_command(cmd) if {
    cmd.command == "kubectl exec"
    cmd.exec_command in ["cat", "nc", "nslookup", "kafka-topics", "env", "ps", "tail", "netstat", "hostname", "ping", "dig", "ip", "ifconfig", "ss", "df", "free", "ls", "whoami", "uptime"]
    not prohibit_sensitive(cmd.full_command)
}

# allow_command(cmd) if {
#    cmd.command == "kubectl delete"
#    cmd.resource_type in ["events", "event", "pods", "pod", "deployment"]
#    # cmd.name in ["checkout*", "frontend*", "app=*", "opentelemetry.io/name=flagd", "otel-collector*", "quote-*", "recommendation*", "ad-*", "cart*", "payment*", "flagd*", "shipping*","coredns*", "product-cata*", "unsupported-checkout*", "frontend-proxy*"]
#    valid_delete_name(cmd.name)
# }


allow_command(cmd) if {
    cmd.command == "kubectl delete"
    cmd.resource_type in ["events", "event", "pods", "pod"]
    valid_delete_name(cmd.name)
}


allow_command(cmd) if {
    cmd.command == "kubectl delete"
    cmd.resource_type == "deployment"
    valid_delete_depy_name(cmd.name)
}

allow_command(cmd) if {
    cmd.command == "kubectl apply"
    cmd.apply_file in [
        "network-policy.yaml",
        "ad-service-deployment.yaml",
        "deployment.yaml",
        "allow-frontend-traffic.yaml",
        "jaeger-deployment.yaml",
        "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml",
        "jaeger-crd.yaml"
    ]
}

# update the policy to allow if input.apply_file in ['anyfile does not start from https/http', or from 'https://github.com/*']

allow_command(cmd) if {
    cmd.command == "kubectl create"
    cmd.ops in ["secrete", "networkpolicy", "namespace", "serviceaccount", "role", "rolebinding"]
}

allow_command(cmd) if {
    cmd.command == "kubectl create"
    cmd.ops in ["deployment", "pod", "service", "configMap"]
    allowed_image(cmd.image)
}

allow_command(cmd) if {
    cmd.command == "kubectl config"
    cmd.ops in ["view", "get-contexts", "current-context", "use-context", "set-context"]
    not prohibit_sensitive(cmd.full_command)
}

allow_command(cmd) if {
    cmd.command == "kubectl scale"
    cmd.replicas > 0
}

allow_command(cmd) if {
    cmd.command == "kubectl patch"
    cmd.legal
    cmd.cpu > 0
    cmd.cpu < 12000
    cmd.memory > 0
    cmd.memory < 16384
    cmd.replicas > 0
    allowed_image(cmd.image)
}

allow_command(cmd) = false if {
    cmd.command == "kubectl apply"
    cmd.apply_file in [
        "https://example.com/new-frontend-proxy-deployment.yaml",
        "/path/to/your/jaeger-crd.yaml",
        "https://raw.githubusercontent.com/jaegertracing/jaeger-openshift/main/all-in-one/jaeger-all-in-one-openshift.yaml",
        "/path/to/frontend-proxy-service.yaml",
        "/path/to/frontend-deployment.yaml"
    ]
}

prohibit_sensitive(full_command) if {
    keyword := ["secret", "certificate", "password", "authorization", "aws_access_key_id", "aws_secret_access_key", "PRIVATE_KEY"][_]
    contains(lower(full_command), lower(keyword))
}

valid_delete_name(name) if {
    prefix := ["checkout", "frontend", "app=", "opentelemetry.io/name=flagd", "otel-collector", "quote-", "recommendation", "ad-", "cart", "payment", "flagd", "shipping","coredns", "product-cata", "unsupported-checkout", "frontend-proxy"][_]
    startswith(name, prefix)
}

valid_delete_depy_name(name) if {
    name in {
        "product-catalog",
        "cart",
        "checkout",
        "ad",
        "frontend-proxy",
        "shipping-service"
    }
}

allowed_image(img) if {
    img == ""  
}

allowed_image(img) if {
    prefix := ["app-image:", "quay.io/it-bench/", "redis:-alpine", "adservice:", "ghcr.io/open-telemetry/", "email-service:", "busybox:", "nginx:", "docker.io/library/", "quay.io/prometheus/"][_]
    startswith(img, prefix)
}

allow_command(cmd) if {
    cmd.command == "kubectl edit"
    allowed_image_in_command(cmd.full_command)
}

allowed_image_in_command(full_command) if {
    img := extract_image_from_patch(full_command)
    allowed_image(img)
}

extract_image_from_patch(full_command) = img if {
    # Example: "image":"quay.io/it-bench/image-name:tag"
    parts := split(full_command, "\"image\":\"")
    count(parts) > 1
    after_image := parts[1]
    img_parts := split(after_image, "\"")
    img := img_parts[0]
}



