import pytest
import asyncio
import tempfile
import shutil
import sys
import os
import uuid
import configparser

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from httpx import AsyncClient
from unittest.mock import MagicMock, patch
from pathlib import Path

from gns3server.controller import Controller
from gns3server.config import Config
from gns3server.compute import MODULES
from gns3server.compute.port_manager import PortManager
from gns3server.compute.project_manager import ProjectManager
from gns3server.db.models import Base, User, Compute
from gns3server.db.repositories.users import UsersRepository
from gns3server.db.repositories.computes import ComputesRepository
from gns3server.api.routes.controller.dependencies.database import get_db_session
from gns3server import schemas
from gns3server.schemas.computes import Protocol
from gns3server.services import auth_service
from gns3server.services.authentication import DEFAULT_JWT_SECRET_KEY

sys._called_from_test = True
sys.original_platform = sys.platform


if sys.platform.startswith("win") and sys.version_info < (3, 8):
    @pytest.fixture(scope="session")
    def event_loop(request):
        """
        Overwrite pytest_asyncio event loop on Windows for Python < 3.8
        As of Python 3.8, the default event loop on Windows is Proactor
        """

        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
        yield loop
        asyncio.set_event_loop(None)


# https://github.com/pytest-dev/pytest-asyncio/issues/68
# this event_loop is used by pytest-asyncio, and redefining it
# is currently the only way of changing the scope of this fixture
@pytest.fixture(scope="class")
def event_loop(request):

    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="class")
async def app() -> FastAPI:

    from gns3server.api.server import app as gns3app
    yield gns3app


@pytest.fixture(scope="class")
async def db_engine():

    db_url = os.getenv("GNS3_TEST_DATABASE_URI", "sqlite+aiosqlite:///:memory:")  # "sqlite:///./sql_test_app.db"
    engine = create_async_engine(db_url, connect_args={"check_same_thread": False}, future=True)
    yield engine
    #await engine.sync_engine.dispose()


@pytest.fixture(scope="class")
async def db_session(db_engine):

    # recreate database tables for each class
    # preferred and faster way would be to rollback the session/transaction
    # but it doesn't work for some reason
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session = AsyncSession(db_engine)
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
async def client(app: FastAPI, db_session: AsyncSession) -> AsyncClient:

    async def _get_test_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db_session] = _get_test_db

    async with AsyncClient(
            app=app,
            base_url="http://test-api",
            headers={"Content-Type": "application/json"}
    ) as client:
        yield client


@pytest.fixture
async def test_user(db_session: AsyncSession) -> User:

    new_user = schemas.UserCreate(
        username="user1",
        email="user1@email.com",
        password="user1_password",
    )
    user_repo = UsersRepository(db_session)
    existing_user = await user_repo.get_user_by_username(new_user.username)
    if existing_user:
        return existing_user
    return await user_repo.create_user(new_user)


@pytest.fixture
async def test_compute(db_session: AsyncSession) -> Compute:

    new_compute = schemas.ComputeCreate(
        compute_id=uuid.uuid4(),
        protocol=Protocol.http,
        host="localhost",
        port=4242,
        user="julien",
        password="secure"
    )

    compute_repo = ComputesRepository(db_session)
    existing_compute = await compute_repo.get_compute(new_compute.compute_id)
    if existing_compute:
        return existing_compute
    return await compute_repo.create_compute(new_compute)


@pytest.fixture
def authorized_client(client: AsyncClient, test_user: User) -> AsyncClient:

    access_token = auth_service.create_access_token(test_user.username)
    client.headers = {
        **client.headers,
        "Authorization": f"Bearer {access_token}",
    }
    return client


@pytest.fixture
def controller_config_path(tmpdir):

    return str(tmpdir / "config" / "gns3_controller.conf")


@pytest.fixture
def controller(tmpdir, controller_config_path):

    Controller._instance = None
    controller = Controller.instance()
    os.makedirs(os.path.dirname(controller_config_path), exist_ok=True)
    Path(controller_config_path).touch()
    controller._config_file = controller_config_path
    controller._config_loaded = True
    return controller


@pytest.fixture
def compute(controller):

    compute = MagicMock()
    compute.id = "example.com"
    controller._computes = {"example.com": compute}
    return compute


@pytest.fixture
@pytest.mark.asyncio
async def project(tmpdir, controller):

    return await controller.add_project(name="Test")


@pytest.fixture
def compute_project(tmpdir):

    return ProjectManager.instance().create_project(project_id="a1e920ca-338a-4e9f-b363-aa607b09dd80")


@pytest.fixture
def config(tmpdir):

    path = str(tmpdir / "server.conf")
    config = configparser.ConfigParser()
    with open(path, "w+") as f:
        config.write(f)
    Config.reset()
    config = Config.instance(files=[path])
    config.clear()
    return config


@pytest.fixture
def images_dir(config):
    """
    Get the location of images
    """

    path = config.settings.Server.images_path
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, "QEMU"), exist_ok=True)
    os.makedirs(os.path.join(path, "IOU"), exist_ok=True)
    return path


@pytest.fixture
def symbols_dir(config):
    """
    Get the location of symbols
    """

    path = config.settings.Server.symbols_path
    os.makedirs(path, exist_ok=True)
    return path


@pytest.fixture
def projects_dir(config):
    """
    Get the location of images
    """

    path = config.settings.Server.projects_path
    os.makedirs(path, exist_ok=True)
    return path


@pytest.fixture(scope="function")
def port_manager():
    """An instance of port manager"""

    PortManager._instance = None
    p = PortManager.instance()
    p.console_host = "127.0.0.1"
    return p


@pytest.fixture(scope="function")
def free_console_port(port_manager, compute_project):
    """Get a free TCP port"""

    # In case of already use ports we will raise an exception
    port = port_manager.get_free_tcp_port(compute_project)
    # We release the port immediately in order to allow
    # the test do whatever the test want
    port_manager.release_tcp_port(port, compute_project)
    return port


@pytest.fixture
def darwin_platform():
    """
    Change sys.plaform to Darwin
    """

    old_platform = sys.platform
    sys.platform = "darwin10.10"
    yield
    sys.plaform = old_platform


@pytest.fixture
def windows_platform():
    """
    Change sys.platform to Windows
    """

    old_platform = sys.platform
    sys.platform = "win10"
    yield
    sys.plaform = old_platform


@pytest.fixture
def linux_platform():
    """
    Change sys.platform to Linux
    """

    old_platform = sys.platform
    sys.platform = "linuxdebian"
    yield
    sys.plaform = old_platform


@pytest.fixture
def on_gns3vm(linux_platform):
    """
    Mock the hostname to  emulate the GNS3 VM
    """

    with patch("gns3server.utils.interfaces.interfaces", return_value=[
            {"name": "eth0", "special": False, "type": "ethernet"},
            {"name": "eth1", "special": False, "type": "ethernet"},
            {"name": "virbr0", "special": True, "type": "ethernet"}]):
        with patch("socket.gethostname", return_value="gns3vm"):
            yield


@pytest.fixture
def ethernet_device():

    import psutil
    return sorted(psutil.net_if_addrs().keys())[0]


@pytest.fixture
def ubridge_path(config):
    """
    Get the location of a fake ubridge
    """

    path = config.settings.Server.ubridge_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'w+').close()
    return path


@pytest.fixture(autouse=True)
def run_around_tests(monkeypatch, config, port_manager):#port_manager, controller, config):
    """
    This setup a temporary project file environment around tests
    """

    tmppath = tempfile.mkdtemp()

    for module in MODULES:
        module._instance = None

    config.settings.Controller.jwt_secret_key = DEFAULT_JWT_SECRET_KEY

    secrets_dir = os.path.join(tmppath, 'secrets')
    os.makedirs(secrets_dir)
    config.settings.Server.secrets_dir = secrets_dir

    projects_dir = os.path.join(tmppath, 'projects')
    os.makedirs(projects_dir)
    config.settings.Server.projects_path = projects_dir

    symbols_dir = os.path.join(tmppath, 'symbols')
    os.makedirs(symbols_dir)
    config.settings.Server.symbols_path = symbols_dir

    images_dir = os.path.join(tmppath, 'images')
    os.makedirs(images_dir)
    config.settings.Server.images_path = images_dir

    appliances_dir = os.path.join(tmppath, 'appliances')
    os.makedirs(appliances_dir)
    config.settings.Server.appliances_path = appliances_dir

    config.settings.Server.ubridge_path = os.path.join(tmppath, 'bin', 'ubridge')
    config.settings.Server.local = True
    config.settings.Server.enable_http_auth = False

    # Prevent executions of the VM if we forgot to mock something
    config.settings.VirtualBox.vboxmanage_path = tmppath
    config.settings.VPCS.vpcs_path = tmppath
    config.settings.VMware.vmrun_path = tmppath
    config.settings.Dynamips.dynamips_path = tmppath


    # Force turn off KVM because it's not available on CI
    config.settings.Qemu.enable_hardware_acceleration = False

    monkeypatch.setattr("gns3server.utils.path.get_default_project_directory", lambda *args: os.path.join(tmppath, 'projects'))

    # Force sys.platform to the original value. Because it seem not be restore correctly at each tests
    sys.platform = sys.original_platform

    yield

    # An helper should not raise Exception
    try:
        shutil.rmtree(tmppath)
    except BaseException:
        pass
