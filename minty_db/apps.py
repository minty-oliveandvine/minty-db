from django.apps import AppConfig


class MintyDbConfig(AppConfig):
    name = "minty_db"
    label = "minty_db"
    verbose_name = "Minty schema"
    default_auto_field = "django.db.models.BigAutoField"
