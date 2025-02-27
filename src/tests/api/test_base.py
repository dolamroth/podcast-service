import json
from json import JSONDecodeError

from httpx import Response
from starlette.testclient import TestClient

from common.models import ModelMixin
from common.statuses import ResponseStatus
from modules.auth.models import UserSession
from modules.auth.views import JWTSessionMixin


class BaseTestCase:
    @staticmethod
    def assert_called_with(mock_callable, *args, **kwargs):
        """Check mock object (callable) on call action with provided `args`, `kwargs`"""

        assert mock_callable.called
        mock_call_args = mock_callable.call_args_list[-1]
        if args:
            assert mock_call_args.args == args
        for key, value in kwargs.items():
            assert key in mock_call_args.kwargs, mock_call_args.kwargs
            assert mock_call_args.kwargs[key] == value

    @staticmethod
    def assert_not_called_with(mock_callable, *args, **kwargs):
        """Check mock object (callable) on call action with provided `args`, `kwargs`"""

        for call_args in mock_callable.call_args_list:
            try:
                asserts = [call_args.args == args]
                asserts.extend(
                    [call_args.kwargs.get(key) == value for key, value in kwargs.items()]
                )
                assert not all(asserts)
            except AssertionError as e:
                raise AssertionError(
                    f"Found unexpected call with args: {args} | kwargs {kwargs}: %r", e
                )


class BaseTestAPIView(BaseTestCase):
    url: str = NotImplemented
    default_fail_status_code = 500
    default_fail_response_status = ResponseStatus.INTERNAL_ERROR

    @staticmethod
    def assert_ok_response(response: Response, status_code: int = 200) -> dict | list:
        assert (
            response.status_code == status_code
        ), f"Unexpected status code. Response: {response.content}"

        try:
            response_data = response.json()
        except JSONDecodeError:
            raise AssertionError(f"Unexpected non-json response: {response.content}")

        assert "payload" in response_data, response_data
        assert response_data["status"] == ResponseStatus.OK
        return response_data["payload"]

    def assert_fail_response(
        self, response: Response, status_code: int = None, response_status: str = None
    ) -> dict | list:
        assert response.status_code == (
            status_code or self.default_fail_status_code
        ), f"Unexpected status code. Response: {response.content}"

        response_data = response.json()
        assert "payload" in response_data, response_data
        assert response_data["status"] == (response_status or self.default_fail_response_status)
        return response_data["payload"]

    @staticmethod
    def assert_bad_request(response: Response, error_details: dict, status_code: int = 400):
        assert (
            response.status_code == status_code
        ), f"Unexpected status code. Response: {response.content}"

        response_data = response.json()
        assert "payload" in response_data, response_data
        response_data = response_data["payload"]
        assert response_data["error"] == "Requested data is not valid."
        for error_field, error_value in error_details.items():
            assert (
                error_field in response_data["details"]
            ), f"{error_field} not found in {response_data['details']}"
            assert (
                error_value in response_data["details"][error_field]
            ), f"{error_value} not found in {response_data['details'][error_field]}"

    @staticmethod
    def assert_not_found(response: Response, instance: ModelMixin):
        assert response.status_code == 404
        response_data = response.json()
        assert response_data["status"] == ResponseStatus.NOT_FOUND
        assert response_data["payload"] == {
            "error": "Requested object not found.",
            "details": (
                f"{instance.__class__.__name__} #{instance.id} "
                f"does not exist or belongs to another user"
            ),
        }

    def assert_unauth(self, response: Response):
        response_data = self.assert_fail_response(
            response, status_code=401, response_status=ResponseStatus.MISSED_CREDENTIALS
        )
        assert response_data == {
            "error": "Authentication is required.",
            "details": "Invalid token header. No credentials provided.",
        }

    def assert_auth_invalid(
        self,
        response_data: Response | dict,
        details: str | None,
        response_status=ResponseStatus.AUTH_FAILED,
    ):
        if isinstance(response_data, Response):
            response_data = self.assert_fail_response(
                response_data, status_code=401, response_status=response_status
            )

        assert response_data == {
            "error": "Authentication credentials are invalid.",
            "details": details,
        }


class BaseTestWSAPI(BaseTestCase):
    url: str = NotImplemented

    @staticmethod
    def _get_headers(user_session: UserSession) -> dict:
        token_col = JWTSessionMixin._get_tokens(user_session.user_id, user_session.public_id)
        return {"Authorization": f"Bearer {token_col.access_token}"}

    def _ws_request(
        self, client: TestClient, user_session: UserSession, data: dict | None = None
    ) -> dict | list:
        data = {"headers": self._get_headers(user_session)} | (data or {})
        with client.websocket_connect(self.url) as websocket:
            websocket.send_json(data)
            response_data = json.loads(websocket.receive_text())

        return response_data
