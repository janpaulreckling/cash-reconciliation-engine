{# Raw tables are append-only version logs. Pick the newest version of each record.
   Filters (e.g. state = 'posted') must be applied AFTER this, never inside it:
   "latest posted version" would resurrect records that were later cancelled. #}
{% macro latest_version(relation, key='id', order_by='_loaded_at desc') -%}
    select * from {{ relation }}
    qualify row_number() over (partition by {{ key }} order by {{ order_by }}) = 1
{%- endmacro %}
