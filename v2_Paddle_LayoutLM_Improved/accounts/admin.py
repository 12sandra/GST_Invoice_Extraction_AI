from django.contrib import admin
from .models import UserProfile

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'company_name', 'gstin', 'created_at')
    search_fields = ('user__username', 'company_name', 'gstin')
