import getpass
import logging
import os
import socket
import time
from urllib.parse import urlparse
import uuid
from weakref import finalize
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = False

import kubernetes

from .objects import make_pod_from_dict, clean_pod_template


logger = logging.getLogger(__name__)


config_file = os.environ.get('CELERYK8S_CONFIG', None)
config = {}
if config_file is not None:
    config_file = Path(config_file).expanduser()
    with open(config_file, 'r') as fh:
        config = yaml.load(fh)


class KubeCluster():
    """ Launch a Celery cluster on Kubernetes

    Dynamically launches celery workers on a Kubernetes cluster.

    **Environments**

    Your worker pod image should have a similar environment to your local
    environment, including versions of Python, celery, and any
    libraries that you may wish to use (like NumPy, Pandas, or Scikit-Learn).
    See examples below for suggestions on how to manage and check for this.

    **Resources**

    Your Kubernetes resource limits and requests should match the
    ``--memory-limit`` and ``--nthreads`` parameters given to the
    ``dask-worker`` command.

    Parameters
    ----------
    pod_template: kubernetes.client.V1PodSpec
        A Kubernetes specification for a Pod for a celery worker.
    name: str (optional)
        Name given to the pods.  Defaults to ``celery-$USER-random``
    namespace: str (optional)
        Namespace in which to launch the workers.
        Defaults to current namespace if available or "default"
    n_workers: int
        Number of workers on initial launch.
        Use ``scale_up`` to increase this number in the future
    env: Dict[str, str]
        Dictionary of environment variables to pass to worker pod
    host: str
        Listen address for local scheduler.  Defaults to 0.0.0.0
    port: int
        Port of local scheduler

    Examples
    --------
    You can also create clusters with worker pod specifications as dictionaries
    or stored in YAML files

    >>> cluster = KubeCluster.from_yaml('worker-template.yml')
    >>> cluster = KubeCluster.from_dict({...})

    Rather than explicitly setting a number of workers you can also ask the
    cluster to allocate workers dynamically based on current workload

    >>> cluster.adapt()

    You can verify that your local environment matches your worker environments
    by calling ``client.get_versions(check=True)``.  This will raise an
    informative error if versions do not match.

    >>> client.get_versions(check=True)

    See Also
    --------
    KubeCluster.from_yaml
    KubeCluster.from_dict
    KubeCluster.adapt
    """
    def __init__(
            self,
            pod_template=None,
            name=None,
            namespace=None,
            n_workers=0,
            env=None,
            app=None,
            log_level='INFO',
            **kwargs
    ):
        if app is None:
            raise ValueError('Reference to the Celery App can not be None.')
        self.app = app

        logging.getLogger().setLevel(log_level)
        logger.setLevel(log_level)

        if pod_template is None:
            if 'kubernetes-worker-template-path' in config:
                import yaml
                with open(config['kubernetes-worker-template-path']) as f:
                    d = yaml.safe_load(f)
                pod_template = make_pod_from_dict(d)
            else:
                msg = ("Worker pod specification not provided. See KubeCluster "
                       "docstring for ways to specify workers")
                raise ValueError(msg)

        try:
            kubernetes.config.load_incluster_config()
        except kubernetes.config.ConfigException:
            kubernetes.config.load_kube_config()

        self.core_api = kubernetes.client.CoreV1Api()

        if namespace is None:
            if 'namespace' in config:
                namespace = config.get('namespace')
            else:
                namespace = _namespace_default()

        if name is None:
            worker_name = config.get('kubernetes-worker-name', 'celery-{user}-{uuid}')
            name = worker_name.format(user=getpass.getuser(), uuid=str(uuid.uuid4())[:10], **os.environ)

        self.pod_template = clean_pod_template(pod_template)

        # Default labels that can't be overwritten
        self.pod_template.metadata.labels['app'] = 'celery'
        self.pod_template.metadata.labels['component'] = 'celery-worker'
        self.pod_template.metadata.namespace = namespace

        if env:
            self.pod_template.spec.containers[0].env.extend([
                kubernetes.client.V1EnvVar(name=k, value=str(v))
                for k, v in env.items()
            ])
        self.pod_template.metadata.generate_name = name

        finalize(self, _cleanup_pods, self.namespace, self.pod_template.metadata.labels)

        if n_workers:
            self.scale(n_workers)

    @classmethod
    def from_dict(cls, pod_spec, **kwargs):
        """ Create cluster with worker pod spec defined by Python dictionary

        Examples
        --------
        >>> spec = {
        ...     'metadata': {},
        ...     'spec': {
        ...         'containers': [{
        ...             'args': ['dask-worker', '$(DASK_SCHEDULER_ADDRESS)',
        ...                      '--nthreads', '1',
        ...                      '--death-timeout', '60'],
        ...             'command': None,
        ...             'image': 'daskdev/dask:latest',
        ...             'name': 'dask-worker',
        ...         }],
        ...     'restartPolicy': 'Never',
        ...     }
        ... }
        >>> cluster = KubeCluster.from_dict(spec, namespace='my-ns')  # doctest: +SKIP

        See Also
        --------
        KubeCluster.from_yaml
        """
        return cls(make_pod_from_dict(pod_spec), **kwargs)

    @classmethod
    def from_yaml(cls, yaml_path, **kwargs):
        """ Create cluster with worker pod spec defined by a YAML file

        We can start a cluster with pods defined in an accompanying YAML file
        like the following:

        .. code-block:: yaml

            kind: Pod
            metadata:
                labels:
                    foo: bar
                    baz: quux
            spec:
                containers:
                -   image: daskdev/dask:latest
                    name: dask-worker
                    args: [dask-worker, $(DASK_SCHEDULER_ADDRESS), --nthreads, '2', --memory-limit, 8GB]
                restartPolicy: Never

        Examples
        --------
        >>> cluster = KubeCluster.from_yaml('pod.yaml', namespace='my-ns')  # doctest: +SKIP

        See Also
        --------
        KubeCluster.from_dict
        """
        if not yaml:
            raise ImportError("PyYaml is required to use yaml functionality, please install it!")
        with open(yaml_path) as f:
            d = yaml.safe_load(f)
            return cls.from_dict(d, **kwargs)

    @property
    def namespace(self):
        return self.pod_template.metadata.namespace

    @property
    def name(self):
        return self.pod_template.metadata.generate_name

    def __repr__(self):
        return 'KubeCluster(workers=%d)' % (len(self.pods()))

    def pods(self):
        """ A list of kubernetes pods corresponding to current workers

        See Also
        --------
        KubeCluster.logs
        """
        return self.core_api.list_namespaced_pod(
            self.namespace,
            label_selector=format_labels(self.pod_template.metadata.labels)
        ).items

    def workers(self):
        return self.app.control.inspect().active()

    def logs(self, pod):
        """ Logs from a worker pod

        You can get this pod object from the ``pods`` method.

        Parameters
        ----------
        pod: kubernetes.client.V1Pod
            The pod from which we want to collect logs.

        See Also
        --------
        KubeCluster.pods
        Client.get_worker_logs
        """
        return self.core_api.read_namespaced_pod_log(pod.metadata.name,
                                                     pod.metadata.namespace)

    def scale(self, n):
        """ Scale cluster to n workers

        Parameters
        ----------
        n: int
            Target number of workers

        Example
        -------
        >>> cluster.scale(10)  # scale cluster to ten workers

        See Also
        --------
        KubeCluster.scale_up
        KubeCluster.scale_down
        """
        pods = self.pods()
        i = self.app.control.inspect()
        workers = i.active()
        workers = workers if workers is not None else []
        logger.info("Scaling to %s workers, current pods %s, current workers %s", n, len(pods), len(workers))

        if len(pods) != len(workers):
            # pods have been deployed but they have not come online yet
            return None

        if n >= len(pods):
            logger.info("New workers: %s >= %s", n, len(pods))
            return self.scale_up(n, pods=pods)
        else:
            to_close = select_workers_to_close(self.app, len(pods) - n)
            logger.info("Closing %s workers: %s", len(to_close), to_close)
            return self.scale_down(to_close)

    def scale_up(self, n, pods=None, **kwargs):
        """
        Make sure we have n dask-workers available for this cluster

        Examples
        --------
        >>> cluster.scale_up(20)  # ask for twenty workers
        """

        pods = pods or self.pods()

        for i in range(3):
            try:
                out = [
                    self.core_api.create_namespaced_pod(self.namespace, self.pod_template)
                    for _ in range(n - len(pods))
                ]
                break
            except kubernetes.client.rest.ApiException as e:
                if e.status == 500 and 'ServerTimeout' in e.body:
                    logger.info("Server timeout, retry #%d", i + 1)
                    time.sleep(1)
                    last_exception = e
                    continue
                else:
                    raise
        else:
            raise last_exception

        return out
        # fixme: wait for this to be ready before returning!

    def scale_down(self, workers):
        """
        When the worker process exits, Kubernetes leaves the pods in a completed
        state. Kill them when we are asked to.

        Parameters
        ----------
        workers: List[str]
            List of addresses of workers to close
        """

        # Get the existing worker pods
        pods = self.pods()

        # Work out pods that we are going to delete
        to_delete = [
            p for p in pods
            # Every time we run, purge any completed pods as well as the specified ones
            if p.status.phase == 'Succeeded' or p.metadata.name in workers
        ]
        if not to_delete:
            return
        for pod in to_delete:
            try:
                self.core_api.delete_namespaced_pod(
                    pod.metadata.name,
                    self.namespace,
                    kubernetes.client.V1DeleteOptions()
                )
                logger.info('Deleted pod: %s', pod.metadata.name)
            except kubernetes.client.rest.ApiException as e:
                # If a pod has already been removed, just ignore the error
                if e.status != 404:
                    raise

    def __enter__(self):
        return self

    def close(self):
        """ Close this cluster """
        self.scale_down([w for w in self.app.control.inspect().active()])
        self.cluster.close()

    def __exit__(self, type, value, traceback):
        _cleanup_pods(self.namespace, self.pod_template.metadata.labels)
        self.cluster.__exit__(type, value, traceback)


def _cleanup_pods(namespace, labels):
    """ Remove all pods with these labels in this namespace """
    api = kubernetes.client.CoreV1Api()
    pods = api.list_namespaced_pod(namespace, label_selector=format_labels(labels))
    for pod in pods.items:
        try:
            api.delete_namespaced_pod(pod.metadata.name, namespace,
                                      kubernetes.client.V1DeleteOptions())
            logger.info('Deleted pod: %s', pod.metadata.name)
        except kubernetes.client.rest.ApiException as e:
            # ignore error if pod is already removed
            if e.status != 404:
                raise


def format_labels(labels):
    """ Convert a dictionary of labels into a comma separated string """
    if labels:
        return ','.join(['{}={}'.format(k, v) for k, v in labels.items()])
    else:
        return ''


def _namespace_default():
    """
    Get current namespace if running in a k8s cluster

    If not in a k8s cluster with service accounts enabled, default to
    'default'

    Taken from https://github.com/jupyterhub/kubespawner/blob/master/kubespawner/spawner.py#L125
    """
    ns_path = '/var/run/secrets/kubernetes.io/serviceaccount/namespace'
    if os.path.exists(ns_path):
        with open(ns_path) as f:
            return f.read().strip()
    return 'default'


def select_workers_to_close(app, n):
    """ Select n workers to close from celery application app """
    i = app.control.inspect()
    workers = i.active()
    assert n <= len(workers), f'Can not scale down to {n} from {len(workers)} workers.'
    key = lambda key: len(key[1])
    to_close = set([w for w, _ in sorted(workers.items(), key=key)][:n])

    if len(to_close) < n:
        rest = sorted(workers.items(), key=key, reverse=True)
        while len(to_close) < n:
            to_close.add(rest.pop()[0])

    logger.debug('Suggest closing workers %s', [(w, len(workers[w])) for w in to_close])
    return [w.replace(f'{app.main}@', '') for w in to_close]
