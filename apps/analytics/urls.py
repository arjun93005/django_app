from django.urls import path

from .views import CategoryTrendView, CheapestPerCategoryView, OverviewView, PriceDropsView

urlpatterns = [
    path("overview/", OverviewView.as_view(), name="analytics-overview"),
    path("price-drops/", PriceDropsView.as_view(), name="analytics-price-drops"),
    path("cheapest/", CheapestPerCategoryView.as_view(), name="analytics-cheapest"),
    path("categories/<slug:slug>/trend/", CategoryTrendView.as_view(), name="analytics-category-trend"),
]
