{# Use the configured schema name as-is (staging, marts, ref) instead of dbt's main_staging. #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name if custom_schema_name else target.schema }}
{%- endmacro %}
