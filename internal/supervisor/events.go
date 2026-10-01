package supervisor

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// KubeEvents writes core/v1 Events, so `kubectl describe` on a cell or a node shows what the
// supervisor did to it and why.
type KubeEvents struct {
	Client   kubernetes.Interface
	Instance string // this replica, as the reporting instance
	Log      *slog.Logger
}

// Event records one Event. A failure to record it is logged, never fatal: the action is done.
func (k *KubeEvents) Event(obj ObjectRef, warning bool, reason, message string) {
	ns := obj.Namespace
	if ns == "" {
		ns = metav1.NamespaceDefault // cluster-scoped objects such as Nodes
	}
	typ := corev1.EventTypeNormal
	if warning {
		typ = corev1.EventTypeWarning
	}
	now := metav1.NewTime(time.Now())
	ev := &corev1.Event{
		ObjectMeta:     metav1.ObjectMeta{GenerateName: fmt.Sprintf("%s.", obj.Name), Namespace: ns},
		InvolvedObject: corev1.ObjectReference{Kind: obj.Kind, Namespace: obj.Namespace, Name: obj.Name, UID: obj.UID},
		Reason:         reason, Message: message, Type: typ, Count: 1,
		FirstTimestamp: now, LastTimestamp: now,
		Source:              corev1.EventSource{Component: "netci-supervisor"},
		ReportingController: "netci.io/supervisor", ReportingInstance: k.Instance,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := k.Client.CoreV1().Events(ns).Create(ctx, ev, metav1.CreateOptions{}); err != nil {
		k.Log.Warn("could not record an event", "reason", reason, "object", obj.Kind+"/"+obj.Name, "error", err)
	}
}
