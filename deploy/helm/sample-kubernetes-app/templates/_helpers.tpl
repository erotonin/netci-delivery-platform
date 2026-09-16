{{- define "sample-kubernetes-app.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "sample-kubernetes-app.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "sample-kubernetes-app.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/* The release's track: an explicit netci.track, else canary/stable from the canary flag. */}}
{{- define "sample-kubernetes-app.track" -}}
{{- if .Values.netci.track }}{{ .Values.netci.track }}{{ else if .Values.canary.enabled }}canary{{ else }}stable{{ end -}}
{{- end }}
