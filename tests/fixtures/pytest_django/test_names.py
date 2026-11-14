import os
from pathlib import Path

import pytest
from django.db import connection


@pytest.mark.django_db
def test_name():
    Path(f"name-{os.getpid()}").write_text(connection.settings_dict["NAME"])
