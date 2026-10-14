{{/*
Expand the name of the chart.
*/}}
{{- define "netci-jenkins-ha.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "netci-jenkins-ha.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "netci-jenkins-ha.labels" -}}
helm.sh/chart: {{ include "netci-jenkins-ha.chart" . }}
{{ include "netci-jenkins-ha.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "netci-jenkins-ha.selectorLabels" -}}
app.kubernetes.io/name: {{ include "netci-jenkins-ha.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Produce replica pod names: jenkins-0..N-1
*/}}
{{- define "netci-jenkins-ha.replicaNames" -}}
{{- $replicas := int .Values.replicas -}}
{{- $names := list -}}
{{- range $i := until $replicas -}}
{{- $names = append $names (printf "jenkins-%d" $i) -}}
{{- end -}}
{{- join "," $names -}}
{{- end }}

{{/*
Produce NETCI_REPLICA_COOKIES: jenkins-0=r0,jenkins-1=r1,...
*/}}
{{- define "netci-jenkins-ha.replicaCookies" -}}
{{- $replicas := int .Values.replicas -}}
{{- $items := list -}}
{{- range $i := until $replicas -}}
{{- $items = append $items (printf "jenkins-%d=r%d" $i $i) -}}
{{- end -}}
{{- join "," $items -}}
{{- end }}

{{/*
Produce NETCI_REPLICA_URLS: jenkins-0=http://jenkins-0.jenkins.<ns>.svc.cluster.local:8080,...
*/}}
{{- define "netci-jenkins-ha.replicaUrls" -}}
{{- $replicas := int .Values.replicas -}}
{{- $ns := .Release.Namespace -}}
{{- $items := list -}}
{{- range $i := until $replicas -}}
{{- $items = append $items (printf "jenkins-%d=http://jenkins-%d.jenkins.%s.svc.cluster.local:8080" $i $i $ns) -}}
{{- end -}}
{{- join "," $items -}}
{{- end }}

{{/*
An image by digest, never by a tag alone: a tag can be moved to other bytes, and the two replicas
of one release must run the same controller. Usage: include "netci-jenkins-ha.image" (list "images.x" .Values.images.x)
*/}}
{{- define "netci-jenkins-ha.image" -}}
{{- $name := index . 0 -}}
{{- $ref := index . 1 -}}
{{- if not $ref -}}
{{- fail (printf "%s: required, an image by digest (repo@sha256:...)" $name) -}}
{{- end -}}
{{- if not (regexMatch "@sha256:[0-9a-f]{64}$" $ref) -}}
{{- fail (printf "%s is %q: it must be pinned by digest (repo@sha256:<64 hex>)" $name $ref) -}}
{{- end -}}
{{- $ref -}}
{{- end }}
