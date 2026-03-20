from django.db import models
from django.contrib.auth.models import User

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    organization = models.CharField(max_length=200, blank=True)
    gstin = models.CharField(max_length=15, blank=True, verbose_name='GSTIN')
    phone = models.CharField(max_length=15, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} - {self.organization}'

    class Meta:
        verbose_name = 'User Profile'
