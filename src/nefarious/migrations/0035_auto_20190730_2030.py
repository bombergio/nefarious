# Generated by Django 2.1.5 on 2019-07-30 20:30

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('nefarious', '0034_auto_20190730_2007'),
    ]

    operations = [
        migrations.CreateModel(
            name='KeywordSearchFilters',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('exclude', models.BooleanField(default=False)),
                ('include', models.BooleanField(default=False)),
            ],
        ),
        migrations.RemoveField(
            model_name='nefarioussettings',
            name='keyword_filters',
        ),
        migrations.AddField(
            model_name='keywordsearchfilters',
            name='settings',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='nefarious.NefariousSettings'),
        ),
    ]
