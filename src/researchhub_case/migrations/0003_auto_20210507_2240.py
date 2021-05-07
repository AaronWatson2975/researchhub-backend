# Generated by Django 2.2 on 2021-05-07 22:40

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('researchhub_case', '0002_auto_20210507_0342'),
    ]

    operations = [
        migrations.AddField(
            model_name='authorclaimcase',
            name='validation_token',
            field=models.CharField(default=None, help_text="Used to authenticate User's identity. See pre_save signal", max_length=32),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='authorclaimcase',
            name='status',
            field=models.CharField(choices=[('CLOSED', 'CLOSED'), ('DENIED', 'DENIED'), ('NULLIFIED', 'NULLIFIED'), ('NULLIFIED', 'NULLIFIED')], default='NULLIFIED', max_length=32),
        ),
    ]
