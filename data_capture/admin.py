from django.contrib import admin
from django.db import models, transaction
from django import forms
from django.core.urlresolvers import reverse
from django.utils.safestring import mark_safe
from django.utils.html import format_html
from django.utils.text import slugify
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm

from . import email
from .schedules import registry
from .models import SubmittedPriceList, SubmittedPriceListRow


class UniqueEmailFormMixin:
    '''
    A mixin that enforces the uniqueness, relative to the User model,
    of an 'email' field in the form it's mixed-in with.

    Taken from https://gist.github.com/gregplaysguitar/1184995.
    '''

    def clean_email(self):
        qs = User.objects.filter(email=self.cleaned_data['email'])
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.count():
            raise forms.ValidationError(
                'That email address is already in use.'
            )
        else:
            return self.cleaned_data['email']


class CustomUserCreationForm(forms.ModelForm, UniqueEmailFormMixin):
    '''
    A substitute for django.contrib.auth.forms.UserCreationForm which
    doesn't ask for information about new users that's irrelevant
    to how CALC works.
    '''

    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ('email',)

    def generate_username(self, email, max_attempts=100):
        '''
        Generate a unique username based on the given email address
        by slugifying the first several characters of the username
        part of the email. If needed, a number is added at the end
        to avoid conflicts with existing usernames.
        '''

        basename = slugify(email.split('@')[0])[:15]
        for i in range(max_attempts):
            if i == 0:
                username = basename
            else:
                username = '{}{}'.format(basename, i)
            if not User.objects.filter(username=username).exists():
                return username
        raise Exception(
            'unable to generate username for {} after {} attempts'.format(
                email,
                max_attempts
            )
        )

    def clean(self):
        email = self.cleaned_data.get('email')

        if email:
            self.cleaned_data['username'] = self.generate_username(email)

        return self.cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data['username']
        if commit:
            user.save()
        return user


class CustomUserChangeForm(UserChangeForm, UniqueEmailFormMixin):
    email = forms.EmailField(required=True)


class CustomUserAdmin(UserAdmin):
    '''
    Simplified user admin for non-superusers, which also prevents such
    users from upgrading themselves to superuser.
    '''

    form = CustomUserChangeForm

    add_form_template = 'admin/data_capture/add_user_form.html'

    add_form = CustomUserCreationForm

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email',),
        }),
    )

    non_superuser_fieldsets = (
        (None, {'fields': (
            'username',
            # Even though we don't need/use the password field, showing it
            # is apparently required to make submitting changes work.
            'password'
        )}),
        ('Personal info', {'fields': ('first_name', 'last_name', 'email')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'groups')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            qs = qs.filter(is_superuser=False)
        return qs

    def get_fieldsets(self, request, obj=None):
        if obj is not None and not request.user.is_superuser:
            return self.non_superuser_fieldsets
        return super().get_fieldsets(request, obj)


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)


class SubmittedPriceListRowInline(admin.TabularInline):
    model = SubmittedPriceListRow

    can_delete = False

    fields = (
        'labor_category',
        'education_level',
        'min_years_experience',
        'base_year_rate',
        'sin',
        'is_muted',
    )

    readonly_fields = ()

    formfield_overrides = {
        models.TextField: {'widget': forms.TextInput}
    }

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status is SubmittedPriceList.STATUS_APPROVED:
            return self.fields
        return self.readonly_fields


@transaction.atomic
def approve(modeladmin, request, queryset):
    unapproved = queryset.exclude(status=SubmittedPriceList.STATUS_APPROVED)
    count = unapproved.count()
    for price_list in unapproved:
        price_list.approve(request.user)
        email.price_list_approved(price_list)

    messages.add_message(
        request,
        messages.INFO,
        '{} price list(s) have been approved and added to CALC.'.format(
            count
        )
    )


approve.short_description = (
    'Approve selected price lists (add their data to CALC)'
)


@transaction.atomic
def unapprove(modeladmin, request, queryset):
    approved = queryset.filter(status=SubmittedPriceList.STATUS_APPROVED)
    count = approved.count()
    for price_list in approved:
        price_list.unapprove(request.user)
        email.price_list_unapproved(price_list)
    messages.add_message(
        request,
        messages.INFO,
        '{} price list(s) have been unapproved and removed from CALC.'.format(
            count
        )
    )


unapprove.short_description = (
    'Unapprove selected price lists (remove their data from CALC)'
)


@transaction.atomic
def reject(modeladmin, request, queryset):
    new_pricelists = queryset.filter(status=SubmittedPriceList.STATUS_NEW)
    count = new_pricelists.count()
    for price_list in new_pricelists:
        price_list.reject(request.user)
        email.price_list_rejected(price_list)
    messages.add_message(
        request,
        messages.INFO,
        '{} price list(s) have been rejected.'.format(count)
    )


reject.short_description = (
    'Reject selected price lists'
)


class UndeletableModelAdmin(admin.ModelAdmin):
    '''
    Represents a model admin UI that offers no way of deleting
    instances. This is useful to ensure accidental data loss, especially
    when we want to keep it around for historical/data provenance purposes.
    '''

    # http://stackoverflow.com/a/25813184/2422398
    def get_actions(self, request):
        actions = super().get_actions(request)
        del actions['delete_selected']
        return actions

    def has_delete_permission(self, request, obj=None):
        return False


class BaseSubmittedPriceListAdmin(UndeletableModelAdmin):
    list_display = ('contract_number', 'vendor_name', 'submitter',
                    'created_at', 'status', 'status_changed_at')

    fields = (
        'contract_number',
        'vendor_name',
        'is_small_business',
        'schedule_title',
        'contractor_site',
        'contract_start',
        'contract_end',
        'submitter',
        'created_at',
        'updated_at',
        'current_status',
        'status_changed_at',
        'status_changed_by',
    )

    readonly_fields = (
        'schedule_title',
        'created_at',
        'updated_at',
        'current_status',
        'submitter',
        'status_changed_at',
        'status_changed_by',
    )

    inlines = [
        SubmittedPriceListRowInline
    ]

    def current_status(self, instance):
        content = instance.get_status_display() + "<br>"
        if instance.status is SubmittedPriceList.STATUS_APPROVED:
            content += ("<span style=\"color: green\">"
                        "This price list has been approved, so its data is "
                        "now in CALC. To unapprove it, you will need to use "
                        "the 'Unapprove selected price lists' action from the "
                        "<a href=\"..\">list view</a>. Note also that in "
                        "order to edit the fields in this price list, you "
                        "will first need to unapprove it.")
        else:
            content += ("<span style=\"color: red\">"
                        "This price list is not currently approved, so its "
                        "data is not in CALC. To approve it, you will need to "
                        "use the 'Approve selected price lists' action from "
                        "the <a href=\"..\">list view</a>.")

        return mark_safe(content)

    def schedule_title(self, instance):
        return registry.get_class(instance.schedule).title

    schedule_title.short_description = 'Schedule'

    def has_add_permission(self, request):
        return False

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status is SubmittedPriceList.STATUS_APPROVED:
            return self.fields
        return self.readonly_fields


class ApprovedPriceList(SubmittedPriceList):
    class Meta:
        proxy = True


@admin.register(ApprovedPriceList)
class ApprovedPriceListAdmin(BaseSubmittedPriceListAdmin):
    actions = [unapprove]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(status=SubmittedPriceList.STATUS_APPROVED)


class NewPriceList(SubmittedPriceList):
    class Meta:
        proxy = True


@admin.register(NewPriceList)
class NewPriceListAdmin(BaseSubmittedPriceListAdmin):
    actions = [approve, reject]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(status=SubmittedPriceList.STATUS_NEW)


class RejectedPriceList(SubmittedPriceList):
    class Meta:
        proxy = True


@admin.register(RejectedPriceList)
class RejectedPriceListAdmin(BaseSubmittedPriceListAdmin):
    actions = [approve]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(status=SubmittedPriceList.STATUS_REJECTED)


class UnapprovedPriceList(SubmittedPriceList):
    class Meta:
        proxy = True


@admin.register(UnapprovedPriceList)
class UnapprovedPriceListAdmin(BaseSubmittedPriceListAdmin):
    actions = [approve]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.filter(status=SubmittedPriceList.STATUS_UNAPPROVED)


@admin.register(SubmittedPriceListRow)
class SubmittedPriceListRowAdmin(UndeletableModelAdmin):
    list_display = (
        'contract_number',
        'vendor_name',
        'labor_category',
        'education_level',
        'min_years_experience',
        'base_year_rate',
        'sin',
        'is_muted',
    )

    list_editable = (
        'is_muted',
    )

    def contract_number(self, obj):
        # get status label and derive url from it
        status_display = obj.price_list.get_status_display()
        url = reverse(
            'admin:data_capture_{}pricelist_change'.format(status_display),
            args=(obj.price_list.id,))
        return format_html(
            '<a href="{}">{}</a>',
            url,
            obj.price_list.contract_number
        )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.exclude(
            price_list__status=SubmittedPriceList.STATUS_APPROVED)

    def vendor_name(self, obj):
        return obj.price_list.vendor_name

    def has_add_permission(self, request):
        return False
