import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import BoardPosition
from apps.community.models import Home, Residency
from apps.core.models import HOASettings


@pytest.fixture(autouse=True)
def hoa(db):
    """Most tests assume first-run setup is already done."""
    settings = HOASettings.load()
    settings.name = "Test Hollow HOA"
    settings.setup_complete = True
    settings.save()
    return settings


@pytest.fixture
def make_user(db):
    def _make(email="person@example.com", password=None, **extra):
        return get_user_model().objects.create_user(email=email, password=password, **extra)

    return _make


@pytest.fixture
def admin_user(db):
    return get_user_model().objects.create_superuser(
        "admin@example.com", "correct-horse-battery", full_name="Ada Admin"
    )


@pytest.fixture
def board_user(make_user):
    user = make_user("treasurer@example.com", "correct-horse-battery", full_name="Tess Treasurer")
    BoardPosition.objects.create(user=user, title=BoardPosition.Title.TREASURER)
    return user


@pytest.fixture
def home(db):
    return Home.objects.create(address="12 Elm Street", lot_number="4")


@pytest.fixture
def other_home(db):
    return Home.objects.create(address="14 Elm Street", lot_number="5")


@pytest.fixture
def resident(make_user, home):
    user = make_user("resident@example.com", full_name="Rae Resident")
    Residency.objects.create(home=home, user=user, is_primary_contact=True)
    return user


@pytest.fixture
def neighbor(make_user, other_home):
    user = make_user("neighbor@example.com", full_name="Ned Neighbor")
    Residency.objects.create(home=other_home, user=user)
    return user


@pytest.fixture
def login(client):
    def _login(user):
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return client

    return _login
