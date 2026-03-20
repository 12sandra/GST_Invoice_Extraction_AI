from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib import messages
from django.views.decorators.http import require_http_methods
from .models import UserProfile


@require_http_methods(['GET', 'POST'])
def login_view(request):
    if request.user.is_authenticated:
        return redirect('converter:dashboard')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        if username and password:
            user = authenticate(request, username=username, password=password)
            if user:
                login(request, user)
                next_url = request.GET.get('next', '')
                return redirect(next_url if next_url else 'converter:dashboard')
            messages.error(request, 'Invalid username or password.')
        else:
            messages.error(request, 'Please enter both username and password.')

    return render(request, 'accounts/login.html')


@require_http_methods(['GET', 'POST'])
def signup_view(request):
    if request.user.is_authenticated:
        return redirect('converter:dashboard')

    if request.method == 'POST':
        username     = request.POST.get('username', '').strip()
        email        = request.POST.get('email', '').strip()
        password1    = request.POST.get('password1', '')
        password2    = request.POST.get('password2', '')
        company_name = request.POST.get('company_name', '').strip()
        gstin        = request.POST.get('gstin', '').strip().upper()

        error = None
        if not username or not password1:
            error = 'Username and password are required.'
        elif len(password1) < 6:
            error = 'Password must be at least 6 characters.'
        elif password1 != password2:
            error = 'Passwords do not match.'
        elif User.objects.filter(username=username).exists():
            error = f'Username "{username}" is already taken. Please choose another.'

        if error:
            messages.error(request, error)
        else:
            try:
                user = User.objects.create_user(
                    username=username, email=email, password=password1)
                UserProfile.objects.get_or_create(
                    user=user,
                    defaults={'company_name': company_name, 'gstin': gstin}
                )
                login(request, user)
                messages.success(request, f'Welcome, {username}! Your account has been created.')
                return redirect('converter:dashboard')
            except Exception as e:
                messages.error(request, f'Could not create account: {e}')

    return render(request, 'accounts/signup.html')


def logout_view(request):
    logout(request)
    return redirect('accounts:login')
