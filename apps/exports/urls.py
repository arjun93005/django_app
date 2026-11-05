from rest_framework.routers import SimpleRouter

from .views import ExportViewSet

router = SimpleRouter()
router.register("", ExportViewSet, basename="export")

urlpatterns = router.urls
