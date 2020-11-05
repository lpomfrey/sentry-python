"""
# GCP Cloud Functions unit tests

"""
import json
from textwrap import dedent
import tempfile
import sys
import subprocess

import pytest
import os.path
import os

pytestmark = pytest.mark.skipif(
    not hasattr(tempfile, "TemporaryDirectory"), reason="need Python 3.2+"
)


FUNCTIONS_PRELUDE = """
from unittest.mock import Mock
import __main__ as gcp_functions
import os

# Initializing all the necessary environment variables
os.environ["FUNCTION_TIMEOUT_SEC"] = "3"
os.environ["FUNCTION_NAME"] = "Google Cloud function"
os.environ["ENTRY_POINT"] = "cloud_function"
os.environ["FUNCTION_IDENTITY"] = "func_ID"
os.environ["FUNCTION_REGION"] = "us-central1"
os.environ["GCP_PROJECT"] = "serverless_project"

gcp_functions.worker_v1 = Mock()
gcp_functions.worker_v1.FunctionHandler = Mock()
gcp_functions.worker_v1.FunctionHandler.invoke_user_function = cloud_function


import sentry_sdk
from sentry_sdk.integrations.gcp import GcpIntegration
import json
import time

from sentry_sdk.transport import HttpTransport

def event_processor(event):
    # Adding delay which would allow us to capture events.
    time.sleep(1)
    return event

def envelope_processor(envelope):
    (item,) = envelope.items
    return item.get_bytes()

def make_assertions():
    # Make any assertions you need to make before the function exits
    pass

class TestTransport(HttpTransport):
    def _send_event(self, event):
        event = event_processor(event)
        # Writing a single string to stdout holds the GIL (seems like) and
        # therefore cannot be interleaved with other threads. This is why we
        # explicitly add a newline at the end even though `print` would provide
        # us one.
        print("\\nEVENT: {}\\n".format(json.dumps(event)))

    def _send_envelope(self, envelope):
        envelope = envelope_processor(envelope)
        print("\\nENVELOPE: {}\\n".format(envelope.decode(\"utf-8\")))

    def flush(self, timeout, callback=None):
        # this is here only because flushing is the last thing we do, so it
        # guarantees that unless we're testing the flushing itself, whatever we
        # need to assert on has already happened
        make_assertions()

        super(TestTransport, self).flush(timeout, callback)

def init_sdk(timeout_warning=False, **extra_init_args):
    sentry_sdk.init(
        dsn="https://123abc@example.com/123",
        transport=TestTransport,
        integrations=[GcpIntegration(timeout_warning=timeout_warning)],
        shutdown_timeout=10,
        **extra_init_args
    )

"""


@pytest.fixture
def run_cloud_function():
    def inner(code, subprocess_kwargs=()):

        event = []
        envelope = []

        # STEP : Create a zip of cloud function

        subprocess_kwargs = dict(subprocess_kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            main_py = os.path.join(tmpdir, "main.py")
            with open(main_py, "w") as f:
                f.write(code)

            setup_cfg = os.path.join(tmpdir, "setup.cfg")

            with open(setup_cfg, "w") as f:
                f.write("[install]\nprefix=")

            subprocess.check_call(
                [sys.executable, "setup.py", "sdist", "-d", os.path.join(tmpdir, "..")],
                **subprocess_kwargs
            )

            subprocess.check_call(
                "pip install ../*.tar.gz -t .",
                cwd=tmpdir,
                shell=True,
                **subprocess_kwargs
            )

            stream = os.popen("python {}/main.py".format(tmpdir))
            stream_data = stream.read()

            stream.close()

            for line in stream_data.splitlines():
                print("GCP:", line)
                if line.startswith("EVENT: "):
                    line = line[len("EVENT: ") :]
                    event = json.loads(line)
                elif line.startswith("ENVELOPE: "):
                    line = line[len("ENVELOPE: ") :]
                    envelope = json.loads(line)
                else:
                    continue

        return envelope, event, stream_data

    return inner


def test_handled_exception(run_cloud_function):
    envelope, event, stream_data = run_cloud_function(
        dedent(
            """
        functionhandler = None
        event = {}
        def cloud_function(functionhandler, event):
            raise Exception("something went wrong")
        """
        )
        + FUNCTIONS_PRELUDE
        + dedent(
            """
        init_sdk(timeout_warning=False)
        gcp_functions.worker_v1.FunctionHandler.invoke_user_function(functionhandler, event)
        """
        )
    )
    assert event["level"] == "error"
    (exception,) = event["exception"]["values"]

    assert exception["type"] == "Exception"
    assert exception["value"] == "something went wrong"
    assert exception["mechanism"] == {"type": "gcp", "handled": False}


def test_unhandled_exception(run_cloud_function):
    envelope, event, stream_data = run_cloud_function(
        dedent(
            """
        functionhandler = None
        event = {}
        def cloud_function(functionhandler, event):
            x = 3/0
            return "3"
        """
        )
        + FUNCTIONS_PRELUDE
        + dedent(
            """
        init_sdk(timeout_warning=False)
        gcp_functions.worker_v1.FunctionHandler.invoke_user_function(functionhandler, event)
        """
        )
    )
    assert event["level"] == "error"
    (exception,) = event["exception"]["values"]

    assert exception["type"] == "ZeroDivisionError"
    assert exception["value"] == "division by zero"
    assert exception["mechanism"] == {"type": "gcp", "handled": False}


def test_timeout_error(run_cloud_function):
    envelope, event, stream_data = run_cloud_function(
        dedent(
            """
        functionhandler = None
        event = {}
        def cloud_function(functionhandler, event):
            time.sleep(10)
            return "3"
        """
        )
        + FUNCTIONS_PRELUDE
        + dedent(
            """
        init_sdk(timeout_warning=True)
        gcp_functions.worker_v1.FunctionHandler.invoke_user_function(functionhandler, event)
        """
        )
    )
    assert event["level"] == "error"
    (exception,) = event["exception"]["values"]

    assert exception["type"] == "ServerlessTimeoutWarning"
    assert (
        exception["value"]
        == "WARNING : Function is expected to get timed out. Configured timeout duration = 3 seconds."
    )
    assert exception["mechanism"] == {"type": "threading", "handled": False}


def test_performance_no_error(run_cloud_function):
    envelope, event, stream_data = run_cloud_function(
        dedent(
            """
        functionhandler = None
        event = {}
        def cloud_function(functionhandler, event):
            return "test_string"
        """
        )
        + FUNCTIONS_PRELUDE
        + dedent(
            """
        init_sdk(traces_sample_rate=1.0)
        gcp_functions.worker_v1.FunctionHandler.invoke_user_function(functionhandler, event)
        """
        )
    )

    assert envelope["type"] == "transaction"
    assert envelope["contexts"]["trace"]["op"] == "serverless.function"
    assert envelope["transaction"].startswith("Google Cloud function")
    assert envelope["transaction"] in envelope["request"]["url"]


def test_performance_error(run_cloud_function):
    envelope, event, stream_data = run_cloud_function(
        dedent(
            """
        functionhandler = None
        event = {}
        def cloud_function(functionhandler, event):
            raise Exception("something went wrong")
        """
        )
        + FUNCTIONS_PRELUDE
        + dedent(
            """
        init_sdk(traces_sample_rate=1.0)
        gcp_functions.worker_v1.FunctionHandler.invoke_user_function(functionhandler, event)
        """
        )
    )

    assert envelope["type"] == "transaction"
    assert envelope["contexts"]["trace"]["op"] == "serverless.function"
    assert envelope["transaction"].startswith("Google Cloud function")
    assert envelope["transaction"] in envelope["request"]["url"]
    assert event["level"] == "error"
    (exception,) = event["exception"]["values"]

    assert exception["type"] == "Exception"
    assert exception["value"] == "something went wrong"
    assert exception["mechanism"] == {"type": "gcp", "handled": False}


def test_traces_sampler_gets_correct_values_in_sampling_context(
    run_cloud_function, DictionaryContaining  # noqa:N803
):
    import inspect

    envelope, event, stream_data = run_cloud_function(
        dedent(
            """
            functionhandler = None
            event = {
                "type": "chase",
                "chasers": ["Maisey", "Charlie"],
                "num_squirrels": 2,
            }
            def cloud_function(functionhandler, event):
                return "dogs are great"
            """
        )
        + FUNCTIONS_PRELUDE
        + dedent(inspect.getsource(DictionaryContaining))
        + dedent(
            """
            os.environ["FUNCTION_NAME"] = "chase_into_tree"
            os.environ["FUNCTION_REGION"] = "dogpark"
            os.environ["GCP_PROJECT"] = "SquirrelChasing"

            import sys
            PY2 = sys.version_info[0] == 2

            try:
                string_types = (str, unicode)  # unicode only exists in python 2

                def dict_values_to_unicode(py2_dict):
                    for key, value in py2_dict.items():
                        if isinstance(value, str):
                            py2_dict[key] = unicode(value)
            except NameError:
                string_types = (str,)

                def dict_values_to_unicode(py3_dict):
                    pass

            def make_assertions():
                try:
                    traces_sampler.assert_any_call(
                        DictionaryContaining({
                            "gcp_env": DictionaryContaining({
                                "function_name": "chase_into_tree",
                                "function_region": "dogpark",
                                "function_project": "SquirrelChasing",
                            }),
                            "gcp_event": {
                                "type": "chase",
                                "chasers": ["Maisey", "Charlie"],
                                "num_squirrels": 2,
                            },
                        })
                    )
                except AssertionError as e:
                    # catch the error and print it because the error itself will
                    # get swallowed by the SDK as an "internal exception"
                    print("\\nAssertionError: {}\\n".format(e))


            traces_sampler = Mock(return_value=True)

            init_sdk(
                traces_sampler=traces_sampler,
                debug=True
            )


            gcp_functions.worker_v1.FunctionHandler.invoke_user_function(functionhandler, event)
            """
        )
    )

    assert "AssertionError" not in str(stream_data)
