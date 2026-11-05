from rest_framework.routers import DefaultRouter

from .views import CrawlRunViewSet, SourceViewSet

router = DefaultRouter()
router.register("sources", SourceViewSet, basename="source")
router.register("runs", CrawlRunViewSet, basename="crawlrun")

urlpatterns = router.urls
