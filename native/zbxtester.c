/*
** trigger-tester native glue around the Zabbix server expression engine.
**
** Reuses the real server code (libzbxeval + evalfunc.c) and replaces only the
** data layer (value cache / trends / config cache) with in-memory stores
** filled from Python through ctypes.
**
** Licensed under the GNU Affero General Public License v3, like Zabbix itself.
*/

#include "zbxcommon.h"
#include "zbxalgo.h"
#include "zbxvariant.h"
#include "zbxhistory.h"
#include "zbxcachevalue.h"
#include "zbxcacheconfig.h"
#include "zbxeval.h"
#include "zbxexpression.h"
#include "zbxstr.h"
#include "zbxnum.h"
#include "zbxtime.h"
#include "zbxtrends.h"
#include "trends.h"
#include "evalfunc.h"

#define ZT_EXPORT __attribute__((visibility("default")))

/* trigger expression syntax with /host/key item references (the form the frontend shows) */
#define ZT_PARSE_RULES	(ZBX_EVAL_PARSE_TRIGGER_EXPRESSION | ZBX_EVAL_PARSE_ITEM_QUERY | ZBX_EVAL_PARSE_COMPOUND_CONST)

/* ------------------------------------------------------------------------ */
/* in-memory item store                                                      */
/* ------------------------------------------------------------------------ */

typedef struct
{
	int	clock;
	int	num;
	double	value_min;
	double	value_avg;
	double	value_max;
}
zt_trend_t;

typedef struct
{
	zbx_uint64_t			itemid;
	unsigned char			value_type;
	char				*host;
	char				*key;
	zbx_vector_history_record_t	records;	/* sorted ascending by timestamp after zt_item_finish() */
	int				sorted;
	zt_trend_t			*trends;
	int				trends_num;
	int				trends_alloc;
}
zt_item_t;

static zt_item_t	*zt_items = NULL;
static int		zt_items_num = 0, zt_items_alloc = 0;
static int		zt_debug = 0;

static zt_item_t	*zt_item_find(zbx_uint64_t itemid)
{
	for (int i = 0; i < zt_items_num; i++)
	{
		if (zt_items[i].itemid == itemid)
			return &zt_items[i];
	}
	return NULL;
}

static zt_item_t	*zt_item_find_by_ref(const char *host, const char *key)
{
	for (int i = 0; i < zt_items_num; i++)
	{
		if (0 == strcmp(zt_items[i].host, host) && 0 == strcmp(zt_items[i].key, key))
			return &zt_items[i];
	}
	return NULL;
}

static int	zt_record_compare_asc(const void *d1, const void *d2)
{
	return zbx_timespec_compare(&((const zbx_history_record_t *)d1)->timestamp,
			&((const zbx_history_record_t *)d2)->timestamp);
}

static void	zt_item_clear(zt_item_t *item)
{
	zbx_history_record_vector_destroy(&item->records, item->value_type);
	zbx_free(item->host);
	zbx_free(item->key);
	zbx_free(item->trends);
}

ZT_EXPORT void	zt_set_debug(int enabled)
{
	zt_debug = enabled;
}

/* ------------------------------------------------------------------------ */
/* virtual clock: nodata() and friends read "now" through zbx_timespec()     */
/* ------------------------------------------------------------------------ */

static zbx_timespec_t	zt_now = {0, 0};

ZT_EXPORT void	zt_set_now(int sec, int ns)
{
	zt_now.sec = sec;
	zt_now.ns = ns;
}

void	zbx_timespec(zbx_timespec_t *ts)
{
	if (0 != zt_now.sec)
	{
		*ts = zt_now;
		return;
	}
	{
		struct timespec	real;

		clock_gettime(CLOCK_REALTIME, &real);
		ts->sec = (int)real.tv_sec;
		ts->ns = (int)real.tv_nsec;
	}
}

ZT_EXPORT void	zt_reset(void)
{
	for (int i = 0; i < zt_items_num; i++)
		zt_item_clear(&zt_items[i]);
	zbx_free(zt_items);
	zt_items_num = zt_items_alloc = 0;
}

ZT_EXPORT int	zt_item_register(zbx_uint64_t itemid, int value_type, const char *host, const char *key)
{
	zt_item_t	*item;

	if (NULL != zt_item_find(itemid))
		return -1;

	if (zt_items_num == zt_items_alloc)
	{
		zt_items_alloc = (0 == zt_items_alloc ? 8 : zt_items_alloc * 2);
		zt_items = (zt_item_t *)zbx_realloc(zt_items, sizeof(zt_item_t) * (size_t)zt_items_alloc);
	}
	item = &zt_items[zt_items_num++];
	memset(item, 0, sizeof(*item));
	item->itemid = itemid;
	item->value_type = (unsigned char)value_type;
	item->host = zbx_strdup(NULL, host);
	item->key = zbx_strdup(NULL, key);
	zbx_history_record_vector_create(&item->records);
	item->sorted = 1;
	return 0;
}

ZT_EXPORT int	zt_item_add_value(zbx_uint64_t itemid, int sec, int ns, const char *value, int log_timestamp,
		const char *log_source, int log_severity, int log_eventid)
{
	zt_item_t		*item;
	zbx_history_record_t	rec;

	if (NULL == (item = zt_item_find(itemid)))
		return -1;

	rec.timestamp.sec = sec;
	rec.timestamp.ns = ns;

	switch (item->value_type)
	{
		case ITEM_VALUE_TYPE_FLOAT:
			rec.value.dbl = atof(value);
			break;
		case ITEM_VALUE_TYPE_UINT64:
			ZBX_STR2UINT64(rec.value.ui64, value);
			break;
		case ITEM_VALUE_TYPE_STR:
		case ITEM_VALUE_TYPE_TEXT:
			rec.value.str = zbx_strdup(NULL, value);
			break;
		case ITEM_VALUE_TYPE_LOG:
			rec.value.log = (zbx_log_value_t *)zbx_malloc(NULL, sizeof(zbx_log_value_t));
			rec.value.log->timestamp = log_timestamp;
			rec.value.log->logeventid = log_eventid;
			rec.value.log->severity = log_severity;
			rec.value.log->source = (NULL != log_source && '\0' != *log_source ?
					zbx_strdup(NULL, log_source) : NULL);
			rec.value.log->value = zbx_strdup(NULL, value);
			break;
		default:
			return -2;
	}
	zbx_vector_history_record_append(&item->records, rec);
	item->sorted = 0;
	return 0;
}

ZT_EXPORT int	zt_item_add_trend(zbx_uint64_t itemid, int clock, int num, double value_min, double value_avg,
		double value_max)
{
	zt_item_t	*item;
	zt_trend_t	*trend;

	if (NULL == (item = zt_item_find(itemid)))
		return -1;

	if (item->trends_num == item->trends_alloc)
	{
		item->trends_alloc = (0 == item->trends_alloc ? 64 : item->trends_alloc * 2);
		item->trends = (zt_trend_t *)zbx_realloc(item->trends, sizeof(zt_trend_t) * (size_t)item->trends_alloc);
	}
	trend = &item->trends[item->trends_num++];
	trend->clock = clock;
	trend->num = num;
	trend->value_min = value_min;
	trend->value_avg = value_avg;
	trend->value_max = value_max;
	return 0;
}

static void	zt_item_sort(zt_item_t *item)
{
	if (0 == item->sorted)
	{
		qsort(item->records.values, (size_t)item->records.values_num, sizeof(zbx_history_record_t),
				zt_record_compare_asc);
		item->sorted = 1;
	}
}

ZT_EXPORT int	zt_item_finish(zbx_uint64_t itemid)
{
	zt_item_t	*item;

	if (NULL == (item = zt_item_find(itemid)))
		return -1;
	zt_item_sort(item);
	return item->records.values_num;
}

/* ------------------------------------------------------------------------ */
/* value cache replacement                                                   */
/*                                                                           */
/* Mirrors vch_item_get_values(): time based requests return values with     */
/* (ts - seconds, ts->ns) < timestamp <= ts, count based requests return the */
/* last <count> values with timestamp <= ts, newest first.                   */
/* ------------------------------------------------------------------------ */

static void	zt_record_copy(zbx_history_record_t *dst, const zbx_history_record_t *src, unsigned char value_type)
{
	dst->timestamp = src->timestamp;

	switch (value_type)
	{
		case ITEM_VALUE_TYPE_STR:
		case ITEM_VALUE_TYPE_TEXT:
			dst->value.str = zbx_strdup(NULL, src->value.str);
			break;
		case ITEM_VALUE_TYPE_LOG:
			dst->value.log = (zbx_log_value_t *)zbx_malloc(NULL, sizeof(zbx_log_value_t));
			*dst->value.log = *src->value.log;
			dst->value.log->source = (NULL != src->value.log->source ?
					zbx_strdup(NULL, src->value.log->source) : NULL);
			dst->value.log->value = zbx_strdup(NULL, src->value.log->value);
			break;
		default:
			dst->value = src->value;
	}
}

int	zbx_vc_get_values(zbx_uint64_t itemid, unsigned char value_type, zbx_vector_history_record_t *values,
		int seconds, int count, const zbx_timespec_t *ts)
{
	zt_item_t	*item;
	zbx_timespec_t	start;
	int		i;

	if (NULL == (item = zt_item_find(itemid)) || item->value_type != value_type)
		return FAIL;

	zt_item_sort(item);
	zbx_vector_history_record_clear(values);

	start.sec = ts->sec - seconds;
	start.ns = ts->ns;

	for (i = item->records.values_num - 1; i >= 0; i--)
	{
		const zbx_history_record_t	*rec = &item->records.values[i];
		zbx_history_record_t		copy;

		if (0 < zbx_timespec_compare(&rec->timestamp, ts))
			continue;

		/* vch_item_get_values_by_time(): the lower bound always applies to time based  */
		/* requests (so a zero period selects nothing); for count based requests only   */
		/* when a time period was given (vch_item_get_values_by_time_and_count())       */
		if ((0 == count || 0 != seconds) && 0 >= zbx_timespec_compare(&rec->timestamp, &start))
			break;

		if (0 != count && values->values_num >= count)
			break;

		zt_record_copy(&copy, rec, value_type);
		zbx_vector_history_record_append(values, copy);
	}

	if (zt_debug)
	{
		fprintf(stderr, "[zbxtester] vc_get_values item:" ZBX_FS_UI64 " seconds:%d count:%d ts:%d.%09d -> %d values\n",
				itemid, seconds, count, ts->sec, ts->ns, values->values_num);
	}
	return SUCCEED;
}

int	zbx_vc_get_value(zbx_uint64_t itemid, unsigned char value_type, const zbx_timespec_t *ts,
		zbx_history_record_t *value)
{
	zbx_vector_history_record_t	values;
	int				ret = FAIL;

	zbx_history_record_vector_create(&values);

	if (SUCCEED == zbx_vc_get_values(itemid, value_type, &values, 0, 1, ts) && 0 < values.values_num)
	{
		*value = values.values[0];
		values.values_num = 0;	/* ownership moved to caller */
		ret = SUCCEED;
	}
	zbx_history_record_vector_destroy(&values, value_type);
	return ret;
}

void	zbx_vc_flush_stats(void)
{
}

/* ------------------------------------------------------------------------ */
/* trends replacement (zbx_trends_eval_* normally run SQL aggregates)        */
/* ------------------------------------------------------------------------ */

static int	zt_trends_eval(zbx_uint64_t itemid, time_t start, time_t end, int func, double *value, char **error)
{
	zt_item_t	*item;
	double		sum = 0, vmin = 0, vmax = 0;
	zbx_uint64_t	num = 0;
	int		found = 0;

	if (NULL == (item = zt_item_find(itemid)))
	{
		*error = zbx_strdup(*error, "item not registered in tester");
		return FAIL;
	}

	for (int i = 0; i < item->trends_num; i++)
	{
		const zt_trend_t	*t = &item->trends[i];

		if (t->clock < start || t->clock > end)	/* server SQL: clock>=start and clock<=end */
			continue;

		if (0 == found)
		{
			vmin = t->value_min;
			vmax = t->value_max;
			found = 1;
		}
		else
		{
			if (t->value_min < vmin)
				vmin = t->value_min;
			if (t->value_max > vmax)
				vmax = t->value_max;
		}
		sum += t->value_avg * t->num;
		num += (zbx_uint64_t)t->num;
	}

	if (0 == found)
	{
		*error = zbx_strdup(*error, "no data");
		return FAIL;
	}

	switch (func)
	{
		case 0: *value = sum / (double)num; break;
		case 1: *value = (double)num; break;
		case 2: *value = vmax; break;
		case 3: *value = vmin; break;
		default: *value = sum;
	}
	return SUCCEED;
}

int	zbx_trends_eval_avg(const char *table, zbx_uint64_t itemid, time_t start, time_t end, double *value, char **error)
{
	ZBX_UNUSED(table);
	return zt_trends_eval(itemid, start, end, 0, value, error);
}

int	zbx_trends_eval_count(const char *table, zbx_uint64_t itemid, time_t start, time_t end, double *value, char **error)
{
	ZBX_UNUSED(table);
	return zt_trends_eval(itemid, start, end, 1, value, error);
}

int	zbx_trends_eval_max(const char *table, zbx_uint64_t itemid, time_t start, time_t end, double *value, char **error)
{
	ZBX_UNUSED(table);
	return zt_trends_eval(itemid, start, end, 2, value, error);
}

int	zbx_trends_eval_min(const char *table, zbx_uint64_t itemid, time_t start, time_t end, double *value, char **error)
{
	ZBX_UNUSED(table);
	return zt_trends_eval(itemid, start, end, 3, value, error);
}

int	zbx_trends_eval_sum(const char *table, zbx_uint64_t itemid, time_t start, time_t end, double *value, char **error)
{
	ZBX_UNUSED(table);
	return zt_trends_eval(itemid, start, end, 4, value, error);
}

/* baseline.c (linked) reads seasonal averages through this entry point; the SQL
 * version in trends.c is renamed at compile time, see build.sh */
zbx_trend_state_t	zbx_trends_get_avg(const char *table, zbx_uint64_t itemid, time_t start, time_t end, double *value)
{
	char	*error = NULL;

	ZBX_UNUSED(table);

	if (SUCCEED == zt_trends_eval(itemid, start, end, 0, value, &error))
		return ZBX_TREND_STATE_NORMAL;

	zbx_free(error);
	return ZBX_TREND_STATE_NODATA;
}

/* global regular expressions (@name) are not available without the configuration cache */
void	zbx_dc_get_expressions_by_name(zbx_vector_expression_t *expressions, const char *name)
{
	ZBX_UNUSED(expressions);
	ZBX_UNUSED(name);
}

/* ------------------------------------------------------------------------ */
/* evaluation API                                                            */
/* ------------------------------------------------------------------------ */

ZT_EXPORT void	zt_free(void *ptr)
{
	free(ptr);
}

/* Evaluates one history function exactly like the server does for a trigger.
 * Returns 0 on success (value in *out_value), 1 on evaluation error (*out_error). */
ZT_EXPORT int	zt_evaluate_function(zbx_uint64_t itemid, const char *function, const char *parameter, int sec, int ns,
		char **out_value, char **out_error)
{
	zt_item_t		*item;
	zbx_dc_evaluate_item_t	evaluate_item;
	zbx_timespec_t		ts = {sec, ns};
	zbx_variant_t		value;
	char			*error = NULL;

	*out_value = NULL;
	*out_error = NULL;
	zt_set_now(sec, ns);

	if (NULL == (item = zt_item_find(itemid)))
	{
		*out_error = zbx_dsprintf(NULL, "item " ZBX_FS_UI64 " is not registered", itemid);
		return 1;
	}

	evaluate_item.itemid = item->itemid;
	evaluate_item.value_type = item->value_type;
	evaluate_item.proxyid = 0;
	evaluate_item.host = item->host;
	evaluate_item.key_orig = item->key;

	zbx_variant_set_none(&value);

	if (SUCCEED != evaluate_function(&value, &evaluate_item, function, parameter, &ts, &error))
	{
		*out_error = zbx_eval_format_function_error(function, item->host, item->key, parameter, error);
		zbx_free(error);
		zbx_variant_clear(&value);
		return 1;
	}

	*out_value = zbx_strdup(NULL, zbx_variant_value_desc(&value));
	zbx_variant_clear(&value);
	return 0;
}

typedef struct
{
	zbx_vector_str_t	refs;
	char			*first_error;
}
zt_eval_data_t;

static int	zt_history_cb(const char *name, size_t len, int args_num, zbx_variant_t *args, void *data,
		const zbx_timespec_t *ts, zbx_variant_t *value, char **error)
{
	zt_eval_data_t		*eval = (zt_eval_data_t *)data;
	zbx_item_query_t	query;
	zt_item_t		*item;
	zbx_dc_evaluate_item_t	evaluate_item;
	char			func_name[MAX_STRING_LEN], *params = NULL, *errmsg = NULL;
	size_t			params_alloc = 0, params_offset = 0;
	int			i, ret;

	zbx_variant_set_none(value);

	if (0 == args_num || ZBX_VARIANT_UI64 != args[0].type)
	{
		*error = zbx_strdup(NULL, "Cannot evaluate function: invalid item query argument");
		return FAIL;
	}
	if (len >= sizeof(func_name))
	{
		*error = zbx_strdup(NULL, "Cannot evaluate function: name too long");
		return FAIL;
	}
	memcpy(func_name, name, len);
	func_name[len] = '\0';

	memset(&query, 0, sizeof(query));
	{
		const char	*ref = eval->refs.values[(int)args[0].data.ui64];

		if (0 == zbx_eval_parse_query(ref, strlen(ref), &query))
		{
			*error = zbx_dsprintf(NULL, "Cannot evaluate function: invalid item query \"%s\"", ref);
			return FAIL;
		}
	}

	if (NULL == query.host || NULL == query.key || NULL != query.filter || 0 == strcmp(query.host, "*"))
	{
		*error = zbx_dsprintf(NULL, "Cannot evaluate function: wildcard/filtered item queries are not"
				" supported by trigger-tester (/%s/%s)", ZBX_NULL2EMPTY_STR(query.host),
				ZBX_NULL2EMPTY_STR(query.key));
		ret = FAIL;
		goto out;
	}

	if (NULL == (item = zt_item_find_by_ref(query.host, query.key)))
	{
		*error = zbx_dsprintf(NULL, "item \"/%s/%s\" is not registered in trigger-tester", query.host, query.key);
		ret = FAIL;
		goto out;
	}

	for (i = 1; i < args_num; i++)
	{
		if (1 != i)
			zbx_chrcpy_alloc(&params, &params_alloc, &params_offset, ',');

		switch (args[i].type)
		{
			case ZBX_VARIANT_DBL:
				zbx_snprintf_alloc(&params, &params_alloc, &params_offset, ZBX_FS_DBL64, args[i].data.dbl);
				break;
			case ZBX_VARIANT_STR:
				zbx_strquote_alloc_opt(&params, &params_alloc, &params_offset, args[i].data.str,
						ZBX_STRQUOTE_DEFAULT);
				break;
			case ZBX_VARIANT_UI64:
				zbx_snprintf_alloc(&params, &params_alloc, &params_offset, ZBX_FS_UI64, args[i].data.ui64);
				break;
			case ZBX_VARIANT_NONE:
				break;
			default:
				*error = zbx_dsprintf(NULL, "unsupported argument #%d type \"%s\"", i,
						zbx_variant_type_desc(&args[i]));
				ret = FAIL;
				goto out;
		}
	}

	evaluate_item.itemid = item->itemid;
	evaluate_item.value_type = item->value_type;
	evaluate_item.proxyid = 0;
	evaluate_item.host = item->host;
	evaluate_item.key_orig = item->key;

	if (SUCCEED != evaluate_function(value, &evaluate_item, func_name, ZBX_NULL2EMPTY_STR(params), ts, &errmsg))
	{
		/* like triggerfunc.c: a failed function becomes an error value and the */
		/* expression engine applies its three-valued logic                     */
		char	*formatted = zbx_eval_format_function_error(func_name, item->host, item->key,
				ZBX_NULL2EMPTY_STR(params), errmsg);

		if (NULL == eval->first_error)
			eval->first_error = zbx_strdup(NULL, formatted);
		zbx_variant_set_error(value, formatted);
		zbx_free(errmsg);
	}
	ret = SUCCEED;
out:
	zbx_free(params);
	zbx_free(query.host);
	zbx_free(query.key);
	zbx_free(query.filter);
	return ret;
}

/* Evaluates a complete trigger expression at the given time.
 * Returns 0 and *result on success, 1 and *out_error when the trigger would become UNKNOWN. */
ZT_EXPORT int	zt_evaluate_expression(const char *expression, int sec, int ns, double *result, char **out_error)
{
	zbx_eval_context_t	ctx;
	zbx_timespec_t		ts = {sec, ns};
	zbx_variant_t		value;
	zt_eval_data_t		data;
	char			*error = NULL;
	int			ret = 1;

	*out_error = NULL;
	zt_set_now(sec, ns);

	if (SUCCEED != zbx_eval_parse_expression(&ctx, expression, ZT_PARSE_RULES, &error))
	{
		*out_error = zbx_dsprintf(NULL, "cannot parse expression: %s", error);
		zbx_free(error);
		return 2;
	}

	zbx_vector_str_create(&data.refs);
	data.first_error = NULL;
	zbx_eval_extract_item_refs(&ctx, &data.refs);
	zbx_variant_set_none(&value);

	if (SUCCEED != zbx_eval_execute_ext(&ctx, &ts, NULL, zt_history_cb, &data, &value, &error))
	{
		*out_error = (NULL != data.first_error && NULL != strstr(error, data.first_error) ?
				zbx_strdup(NULL, error) : zbx_dsprintf(NULL, "%s", error));
		zbx_free(error);
		goto out;
	}

	if (SUCCEED != zbx_variant_convert(&value, ZBX_VARIANT_DBL))
	{
		*out_error = zbx_dsprintf(NULL, "Cannot convert expression result of type \"%s\" to floating point value",
				zbx_variant_type_desc(&value));
		goto out;
	}

	*result = value.data.dbl;
	ret = 0;
out:
	zbx_variant_clear(&value);
	zbx_free(data.first_error);
	zbx_vector_str_clear_ext(&data.refs, zbx_str_free);
	zbx_vector_str_destroy(&data.refs);
	zbx_eval_clear(&ctx);
	return ret;
}

ZT_EXPORT const char	*zt_zabbix_version(void)
{
	return ZABBIX_VERSION;
}

#ifndef ZT_SOURCE_TAG
#	define ZT_SOURCE_TAG "unknown"
#endif

/* "7.0.32" - the git tag the library was built from */
ZT_EXPORT const char	*zt_source_tag(void)
{
	return ZT_SOURCE_TAG;
}

/* ------------------------------------------------------------------------ */
/* database / cache stubs: never reached through the paths used above, but   */
/* required to link evalfunc.o, history_sql.o and trends.o                   */
/* ------------------------------------------------------------------------ */

#include "zbxdbhigh.h"
#include "zbxcachehistory.h"

zbx_db_result_t	zbx_db_select(const char *fmt, ...)
{
	ZBX_UNUSED(fmt);
	if (zt_debug)
		fprintf(stderr, "[zbxtester] zbx_db_select() called: no database in tester\n");
	return NULL;
}

zbx_db_result_t	zbx_db_select_n(const char *query, int n)
{
	ZBX_UNUSED(query); ZBX_UNUSED(n);
	return NULL;
}

zbx_db_row_t	zbx_db_fetch(zbx_db_result_t result)
{
	ZBX_UNUSED(result);
	return NULL;
}

void	zbx_db_free_result(zbx_db_result_t result)
{
	ZBX_UNUSED(result);
}

int	zbx_db_is_null(const char *field)
{
	return (NULL == field ? SUCCEED : FAIL);
}

void	zbx_db_begin(void)
{
}

int	zbx_db_commit(void)
{
	return FAIL;
}

zbx_err_codes_t	zbx_db_last_errcode(void)
{
	return ERR_Z3001;
}

void	zbx_db_insert_prepare(zbx_db_insert_t *self, const char *table, ...)
{
	ZBX_UNUSED(self); ZBX_UNUSED(table);
}

void	zbx_db_insert_add_values(zbx_db_insert_t *self, ...)
{
	ZBX_UNUSED(self);
}

int	zbx_db_insert_execute(zbx_db_insert_t *self)
{
	ZBX_UNUSED(self);
	return FAIL;
}

void	zbx_db_insert_clean(zbx_db_insert_t *self)
{
	ZBX_UNUSED(self);
}

void	zbx_recalc_time_period(time_t *ts_from, int table_group)
{
	ZBX_UNUSED(ts_from); ZBX_UNUSED(table_group);
}

/* item data is "expected" from the beginning of time: nodata() never suppresses after start */
int	zbx_dc_get_data_expected_from(zbx_uint64_t itemid, int *seconds)
{
	ZBX_UNUSED(itemid);
	*seconds = 0;
	return SUCCEED;
}

int	zbx_dc_get_proxy_nodata_win(zbx_uint64_t hostid, zbx_proxy_suppress_t *nodata_win, int *lastaccess)
{
	ZBX_UNUSED(hostid); ZBX_UNUSED(nodata_win); ZBX_UNUSED(lastaccess);
	return FAIL;
}

int	zbx_hc_is_itemid_cached_and_normal(zbx_uint64_t itemid, int period_start)
{
	ZBX_UNUSED(itemid); ZBX_UNUSED(period_start);
	return FAIL;
}

/* trend function cache (shared memory) is only used by the renamed SQL trend functions */

int	zbx_tfc_get_value(zbx_uint64_t itemid, time_t start, time_t end, zbx_trend_function_t function, double *value,
		zbx_trend_state_t *state)
{
	ZBX_UNUSED(itemid); ZBX_UNUSED(start); ZBX_UNUSED(end); ZBX_UNUSED(function); ZBX_UNUSED(value);
	ZBX_UNUSED(state);
	return FAIL;
}

void	zbx_tfc_put_value(zbx_uint64_t itemid, time_t start, time_t end, zbx_trend_function_t function, double value,
		zbx_trend_state_t state)
{
	ZBX_UNUSED(itemid); ZBX_UNUSED(start); ZBX_UNUSED(end); ZBX_UNUSED(function); ZBX_UNUSED(value);
	ZBX_UNUSED(state);
}
