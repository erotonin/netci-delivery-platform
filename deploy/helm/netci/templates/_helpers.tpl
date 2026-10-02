{{- define "netci.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "netci.image" -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" (.Values.image.digest | default "")) -}}
{{- fail "image.digest is required and must be sha256: followed by 64 hex digits (images are pinned by digest)" -}}
{{- end -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- end -}}

{{/* netci-fabric's configuration. The chart decides the fields set here; whatever the user sets for
them is overwritten. Used by the ConfigMap and by the Deployment's checksum. */}}
{{- define "netci.fabricConfig" -}}
{{- $config := mergeOverwrite (deepCopy .Values.fabric.config) (dict "namespace" .Values.fabric.sandboxNamespace "serviceAccount" "netci-sandbox" "audience" "netci-fabric" "bootstrapImage" (include "netci.image" .) "fabricUrl" (printf "http://netci-fabric.%s.svc.cluster.local:8080" .Release.Namespace)) }}
{{- if .Values.priorityClasses.create }}{{ $_ := set $config "priorityClass" "netci-sandbox" }}{{ end }}
{{- $config | toJson }}
{{- end }}
