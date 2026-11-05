import csv
import io

import pytest
from django.contrib.auth import get_user_model

from apps.exports.models import Export

from .conftest import make_product


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user("analyst", password="pw")


@pytest.fixture
def products(source, category):
    make_product(source, external_id="A", price="10.00", category=category, title='Comma, "quoted" title')
    make_product(source, external_id="B", price="20.00", category=category, in_stock=False)


def create_export(api_client, user, django_capture_on_commit_callbacks, filters=None):
    api_client.force_authenticate(user)
    with django_capture_on_commit_callbacks(execute=True):  # runs the (eager) Celery task
        response = api_client.post("/api/v1/exports/", {"filters": filters or {}}, format="json")
    assert response.status_code == 202, response.content
    return Export.objects.get(pk=response.json()["id"])


@pytest.mark.django_db
def test_export_is_generated_with_copy_and_downloadable(api_client, user, products, django_capture_on_commit_callbacks):
    export = create_export(api_client, user, django_capture_on_commit_callbacks, {"in_stock": True})

    assert export.status == "ready"
    assert export.row_count == 1
    response = api_client.get(f"/api/v1/exports/{export.pk}/download/")
    rows = list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode())))
    assert rows[0]["external_id"] == "A"
    assert rows[0]["title"] == 'Comma, "quoted" title'  # proper CSV quoting from COPY
    assert rows[0]["current_price"] == "10.00"


@pytest.mark.django_db
def test_download_delegates_to_nginx_with_x_accel_redirect(
    api_client, user, products, settings, django_capture_on_commit_callbacks
):
    export = create_export(api_client, user, django_capture_on_commit_callbacks)
    settings.EXPORTS_USE_X_ACCEL = True

    response = api_client.get(f"/api/v1/exports/{export.pk}/download/")

    assert response["X-Accel-Redirect"] == f"/protected-exports/{export.file_name}"
    assert response["Content-Disposition"] == f'attachment; filename="{export.file_name}"'
    assert response.content == b""


@pytest.mark.django_db
def test_exports_are_private_to_their_owner(api_client, user, products, django_capture_on_commit_callbacks):
    export = create_export(api_client, user, django_capture_on_commit_callbacks)
    intruder = get_user_model().objects.create_user("intruder", password="pw")
    api_client.force_authenticate(intruder)
    assert api_client.get(f"/api/v1/exports/{export.pk}/download/").status_code == 404


@pytest.mark.django_db
def test_unknown_filters_are_rejected(api_client, user):
    api_client.force_authenticate(user)
    response = api_client.post("/api/v1/exports/", {"filters": {"1=1; DROP TABLE": "x"}}, format="json")
    assert response.status_code == 400


@pytest.mark.django_db
def test_anonymous_users_cannot_export(api_client):
    assert api_client.post("/api/v1/exports/", {}, format="json").status_code == 401
