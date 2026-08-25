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
