{{/* Expand the name of the chart. */}}
{{- define "netci.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* A fully qualified app name, shortened for DNS. */}}
{{- define "netci.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "netci.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: netci-platform
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}

{{/* Selector labels for one component: api, worker or portal. */}}
{{- define "netci.selectorLabels" -}}
app.kubernetes.io/name: {{ include "netci.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* <registry>/<component>:<tag>. The registry is required: there is no public image. */}}
{{- define "netci.image" -}}
{{- $registry := required "image.registry is required: where the netCI images were pushed (scripts/build_images.sh)" .root.Values.image.registry -}}
{{- printf "%s/%s:%s" (trimSuffix "/" $registry) .component (default .root.Chart.AppVersion .root.Values.image.tag) -}}
{{- end }}

{{- define "netci.secretName" -}}
{{- default (printf "%s-app" (include "netci.fullname" .)) .Values.existingSecret -}}
{{- end }}

{{- define "netci.externalUrl" -}}
{{- required "global.externalUrl is required: the https:// address people and Jenkins reach netCI at" .Values.global.externalUrl | trimSuffix "/" -}}
{{- end }}

{{/* The API inside the cluster: what the worker reports results to. */}}
{{- define "netci.apiInternalUrl" -}}
{{- printf "http://%s-api.%s.svc:8000" (include "netci.fullname" .) .Release.Namespace -}}
{{- end }}

{{/*
Where Jenkins reports builds. Jenkins is usually outside the cluster, so the default is
the external address people use, under /api where the portal proxies the API.
*/}}
{{- define "netci.callbackUrl" -}}
{{- if .Values.jenkins.callbackUrl -}}
{{- .Values.jenkins.callbackUrl | trimSuffix "/" -}}
{{- else -}}
{{- printf "%s/api" (include "netci.externalUrl" .) -}}
{{- end -}}
{{- end }}

{{/* The secret file name for a controller's API token. */}}
{{- define "netci.jenkinsTokenKey" -}}
{{- printf "jenkins-%s-api-token" (lower .) -}}
{{- end }}

{{/* Environment variable prefix for a controller: JENKINS_<ID>. */}}
{{- define "netci.jenkinsPrefix" -}}
{{- printf "JENKINS_%s" (upper . | replace "-" "_") -}}
{{- end }}

{{/*
Pod-level hardening every netCI container shares: no root, no privilege escalation,
nothing the runtime does not strictly need, and the default seccomp profile.
*/}}
{{- define "netci.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "netci.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}
