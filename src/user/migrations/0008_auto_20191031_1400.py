# Generated by Django 2.2.6 on 2019-10-31 14:00

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0007_auto_20191023_2047'),
    ]

    operations = [
        migrations.RenameField(
            model_name='author',
            old_name='created_at',
            new_name='created_date',
        ),
        migrations.RenameField(
            model_name='author',
            old_name='updated_at',
            new_name='updated_date',
        ),
        migrations.RenameField(
            model_name='university',
            old_name='created_at',
            new_name='created_date',
        ),
        migrations.RenameField(
            model_name='university',
            old_name='updated_at',
            new_name='updated_date',
        ),
        migrations.RenameField(
            model_name='user',
            old_name='created_at',
            new_name='created_date',
        ),
        migrations.RenameField(
            model_name='user',
            old_name='updated_at',
            new_name='updated_date',
        ),
    ]
