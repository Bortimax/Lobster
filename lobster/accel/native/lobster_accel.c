/* Native implementations of the two seam kernels (D26's seam, D42).
 *
 * Plain C against the CPython API, on purpose:
 *
 *   - it needs no build dependency at all - no Cython, no pybind11, no cargo -
 *     so any machine that can build CPython extensions can build this, and CI
 *     needs `pip install .` and nothing else;
 *   - the kernels are a few hundred lines of double arithmetic over fixed-size
 *     vectors, which is the case where C's ergonomic cost is lowest.
 *
 * The maths mirrors `lobster/geometry.py` branch for branch. That is not
 * stylistic: `lobster.conformance.AUTHORITY` is "python", so where this and the
 * reference disagree beyond the declared tolerance, THIS is wrong. Any change
 * here that is not a change there is a divergence waiting to be found.
 *
 * Two things that look like details and are not:
 *
 *   - candidates sort by (distance, entity_id), not by distance alone;
 *   - nearest_region breaks ties towards the EARLIER capsule, because the
 *     reference compares with a strict `<`. Ordering is part of the input.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

static const double EPS = 1e-12;

typedef struct { double x, y, z; } Vec3;

static Vec3 vsub(Vec3 a, Vec3 b) { Vec3 r = {a.x-b.x, a.y-b.y, a.z-b.z}; return r; }
static double vdot(Vec3 a, Vec3 b) { return a.x*b.x + a.y*b.y + a.z*b.z; }
static double vlen(Vec3 a) { return sqrt(vdot(a, a)); }

static double clamp01(double v) { return v < 0.0 ? 0.0 : (v > 1.0 ? 1.0 : v); }

/* Read a 3-element sequence of floats. Returns 0 on failure with an
 * exception set - never a partially filled vector, because a silently
 * zero-filled coordinate is exactly the kind of wrong answer that would pass
 * the differential suite on most cases and fail on one. */
static int read_vec3(PyObject *seq, Vec3 *out)
{
    PyObject *fast = PySequence_Fast(seq, "expected a 3-element sequence");
    if (!fast) return 0;
    if (PySequence_Fast_GET_SIZE(fast) < 3) {
        Py_DECREF(fast);
        PyErr_SetString(PyExc_ValueError, "expected 3 coordinates");
        return 0;
    }
    PyObject **items = PySequence_Fast_ITEMS(fast);
    double c[3];
    for (int i = 0; i < 3; ++i) {
        c[i] = PyFloat_AsDouble(items[i]);
        if (c[i] == -1.0 && PyErr_Occurred()) { Py_DECREF(fast); return 0; }
    }
    Py_DECREF(fast);
    out->x = c[0]; out->y = c[1]; out->z = c[2];
    return 1;
}

/* geometry.closest_point_on_segment, distance half only. */
static double point_segment_distance(Vec3 a, Vec3 b, Vec3 p)
{
    Vec3 ab = vsub(b, a);
    double denom = vdot(ab, ab);
    if (denom == 0.0) return vlen(vsub(a, p));
    double t = clamp01(vdot(vsub(p, a), ab) / denom);
    Vec3 closest = { a.x + ab.x*t, a.y + ab.y*t, a.z + ab.z*t };
    return vlen(vsub(closest, p));
}

/* geometry.segment_segment_distance - the standard clamped-parameter
 * solution, with the same degenerate branches in the same order. */
static double segment_segment_distance(Vec3 p1, Vec3 q1, Vec3 p2, Vec3 q2)
{
    Vec3 d1 = vsub(q1, p1);
    Vec3 d2 = vsub(q2, p2);
    Vec3 r  = vsub(p1, p2);
    double a = vdot(d1, d1);
    double e = vdot(d2, d2);
    double f = vdot(d2, r);
    double s = 0.0, t = 0.0;

    if (a <= EPS && e <= EPS) return vlen(vsub(p1, p2));
    if (a <= EPS) {
        s = 0.0;
        t = clamp01(f / e);
    } else {
        double c = vdot(d1, r);
        if (e <= EPS) {
            t = 0.0;
            s = clamp01(-c / a);
        } else {
            double b = vdot(d1, d2);
            double denom = a * e - b * b;
            s = (denom != 0.0) ? clamp01((b * f - c * e) / denom) : 0.0;
            t = (b * s + f) / e;
            if (t < 0.0)      { t = 0.0; s = clamp01(-c / a); }
            else if (t > 1.0) { t = 1.0; s = clamp01((b - c) / a); }
        }
    }
    Vec3 c1 = { p1.x + d1.x*s, p1.y + d1.y*s, p1.z + d1.z*s };
    Vec3 c2 = { p2.x + d2.x*t, p2.y + d2.y*t, p2.z + d2.z*t };
    return vlen(vsub(c1, c2));
}

/* ---------------------------------------------------------------------- */
/* segment_query                                                          */
/* ---------------------------------------------------------------------- */

typedef struct { PyObject *id; double distance; } Hit;

static int hit_cmp(const void *lhs, const void *rhs)
{
    const Hit *a = (const Hit *)lhs, *b = (const Hit *)rhs;
    if (a->distance < b->distance) return -1;
    if (a->distance > b->distance) return 1;
    /* Ties break by entity id, matching the reference's
     * `key=lambda c: (c.distance, c.entity_id)`. Without this the two paths
     * agree on membership and disagree on order, which the comparator's
     * ordering rule is written to catch. */
    const char *ai = PyUnicode_AsUTF8(a->id);
    const char *bi = PyUnicode_AsUTF8(b->id);
    if (!ai || !bi) return 0;
    return strcmp(ai, bi);
}

static PyObject *segment_query(PyObject *self, PyObject *payload)
{
    PyObject *entries = NULL, *start_o = NULL, *end_o = NULL;
    PyObject *radius_o = NULL, *tiers_o = NULL;
    PyObject *result = NULL, *hits = NULL, *fast = NULL;
    Hit *found = NULL;
    Vec3 start, end;

    entries = PyMapping_GetItemString(payload, "entries");
    if (!entries) goto done;
    start_o = PyMapping_GetItemString(payload, "start");
    if (!start_o || !read_vec3(start_o, &start)) goto done;
    end_o = PyMapping_GetItemString(payload, "end");
    if (!end_o || !read_vec3(end_o, &end)) goto done;
    radius_o = PyMapping_GetItemString(payload, "radius");
    if (!radius_o) goto done;
    double radius = PyFloat_AsDouble(radius_o);
    if (radius == -1.0 && PyErr_Occurred()) goto done;

    tiers_o = PyMapping_GetItemString(payload, "tiers");
    if (!tiers_o) PyErr_Clear();
    int filtering = (tiers_o && tiers_o != Py_None
                     && PyObject_IsTrue(tiers_o) == 1);

    fast = PySequence_Fast(entries, "entries must be a sequence");
    if (!fast) goto done;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(fast);
    PyObject **rows = PySequence_Fast_ITEMS(fast);

    found = (Hit *)PyMem_Malloc((n ? n : 1) * sizeof(Hit));
    if (!found) { PyErr_NoMemory(); goto done; }
    Py_ssize_t count = 0;

    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject *row = PySequence_Fast(rows[i], "entry must be a sequence");
        if (!row) goto done;
        if (PySequence_Fast_GET_SIZE(row) < 3) {
            Py_DECREF(row);
            PyErr_SetString(PyExc_ValueError, "entry needs (id, position, tier)");
            goto done;
        }
        PyObject **cells = PySequence_Fast_ITEMS(row);
        if (filtering) {
            int in = PySequence_Contains(tiers_o, cells[2]);
            if (in < 0) { Py_DECREF(row); goto done; }
            if (!in) { Py_DECREF(row); continue; }
        }
        Vec3 p;
        if (!read_vec3(cells[1], &p)) { Py_DECREF(row); goto done; }
        double d = point_segment_distance(start, end, p);
        if (d <= radius) {
            found[count].id = cells[0];
            Py_INCREF(found[count].id);
            found[count].distance = d;
            count += 1;
        }
        Py_DECREF(row);
    }

    qsort(found, (size_t)count, sizeof(Hit), hit_cmp);

    hits = PyList_New(count);
    if (!hits) goto done;
    for (Py_ssize_t i = 0; i < count; ++i) {
        PyObject *pair = Py_BuildValue("[Od]", found[i].id, found[i].distance);
        if (!pair) { Py_CLEAR(hits); goto done; }
        PyList_SET_ITEM(hits, i, pair);
    }
    result = Py_BuildValue("{s:N}", "hits", hits);
    hits = NULL;

done:
    if (found) {
        for (Py_ssize_t i = 0; i < 0; ++i) { (void)i; }
        PyMem_Free(found);
    }
    Py_XDECREF(hits);
    Py_XDECREF(fast);
    Py_XDECREF(tiers_o);
    Py_XDECREF(radius_o);
    Py_XDECREF(end_o);
    Py_XDECREF(start_o);
    Py_XDECREF(entries);
    return result;
}

/* ---------------------------------------------------------------------- */
/* nearest_region                                                         */
/* ---------------------------------------------------------------------- */

static PyObject *nearest_region(PyObject *self, PyObject *payload)
{
    PyObject *boxes = NULL, *origin_o = NULL, *dir_o = NULL, *max_o = NULL;
    PyObject *result = NULL, *fast = NULL;
    Vec3 origin, direction;

    boxes = PyMapping_GetItemString(payload, "boxes");
    if (!boxes) goto done;
    origin_o = PyMapping_GetItemString(payload, "origin");
    if (!origin_o || !read_vec3(origin_o, &origin)) goto done;
    dir_o = PyMapping_GetItemString(payload, "direction");
    if (!dir_o || !read_vec3(dir_o, &direction)) goto done;
    max_o = PyMapping_GetItemString(payload, "max_distance");
    if (!max_o) goto done;
    double max_distance = PyFloat_AsDouble(max_o);
    if (max_distance == -1.0 && PyErr_Occurred()) goto done;

    fast = PySequence_Fast(boxes, "boxes must be a sequence");
    if (!fast) goto done;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(fast);
    if (n == 0) {
        result = Py_BuildValue("{s:O,s:O}", "region", Py_None,
                               "precise", Py_False);
        goto done;
    }
    PyObject **rows = PySequence_Fast_ITEMS(fast);

    /* One reach for both questions - see D41. The reference normalises here
     * and `ray_capsule_hit` normalises too; they used to disagree. */
    double len = vlen(direction);
    Vec3 heading = (len != 0.0)
        ? (Vec3){ direction.x/len, direction.y/len, direction.z/len }
        : (Vec3){ 0.0, 0.0, 0.0 };
    Vec3 far_pt = { origin.x + heading.x * max_distance,
                    origin.y + heading.y * max_distance,
                    origin.z + heading.z * max_distance };

    PyObject *best_hit = NULL, *best_near = NULL;
    double best_along = 0.0, best_gap = 0.0;

    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject *row = PySequence_Fast(rows[i], "box must be a sequence");
        if (!row) goto done;
        if (PySequence_Fast_GET_SIZE(row) < 4) {
            Py_DECREF(row);
            PyErr_SetString(PyExc_ValueError, "box needs (region, a, b, radius)");
            goto done;
        }
        PyObject **cells = PySequence_Fast_ITEMS(row);
        Vec3 a, b;
        if (!read_vec3(cells[1], &a) || !read_vec3(cells[2], &b)) {
            Py_DECREF(row); goto done;
        }
        double cap_radius = PyFloat_AsDouble(cells[3]);
        if (cap_radius == -1.0 && PyErr_Occurred()) { Py_DECREF(row); goto done; }

        double gap = segment_segment_distance(origin, far_pt, a, b);
        Vec3 centre = { (a.x+b.x)*0.5, (a.y+b.y)*0.5, (a.z+b.z)*0.5 };
        double along = vlen(vsub(origin, centre));

        /* Strictly `<`, so the earlier capsule wins a tie. */
        if (gap <= cap_radius && (best_hit == NULL || along < best_along)) {
            best_hit = cells[0];
            best_along = along;
        }
        if (best_near == NULL || gap < best_gap) {
            best_near = cells[0];
            best_gap = gap;
        }
        Py_DECREF(row);
    }

    if (best_hit) {
        result = Py_BuildValue("{s:O,s:O}", "region", best_hit,
                               "precise", Py_True);
    } else {
        result = Py_BuildValue("{s:O,s:O}", "region",
                               best_near ? best_near : Py_None,
                               "precise", Py_False);
    }

done:
    Py_XDECREF(fast);
    Py_XDECREF(max_o);
    Py_XDECREF(dir_o);
    Py_XDECREF(origin_o);
    Py_XDECREF(boxes);
    return result;
}

/* ---------------------------------------------------------------------- */

static PyMethodDef methods[] = {
    {"segment_query", segment_query, METH_O,
     "Entities within radius of a segment, nearest first."},
    {"nearest_region", nearest_region, METH_O,
     "Which bone a ray struck, over an already-filtered capsule list."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "lobster_accel",
    "Native seam kernels for Lobster (DECISIONS.md D26, D42).",
    -1, methods, NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC PyInit_lobster_accel(void)
{
    return PyModule_Create(&module);
}
