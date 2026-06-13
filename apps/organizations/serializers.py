from rest_framework import serializers

from apps.organizations.models import Subsidiary


class SubsidiarySerializer(serializers.ModelSerializer):
    company = serializers.PrimaryKeyRelatedField(
        queryset=Subsidiary._meta.get_field("company").related_model.objects.all(),
        required=False,
    )
    company_name = serializers.CharField(source="company.name", read_only=True)

    class Meta:
        model = Subsidiary
        fields = ["id", "name", "code", "city", "address", "email", "phone",
                  "company", "company_name", "is_active"]
