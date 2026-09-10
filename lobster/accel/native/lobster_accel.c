/* Native implementations of the three seam kernels (D26's seam, D42, D53).
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

/* Mirrors `conformance.PLACEMENT_FLOATS` / `INSTANCE_FLOATS`. A test
 * asserts these agree, because a shape constant in two files is a
 * shape constant that will differ. */
#define PLACEMENT_FLOATS 7
#define INSTANCE_FLOATS 17

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

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
    /* Declared and initialised before the first `goto done`: jumping over the
     * initialisation of a scalar leaves it indeterminate, and the cleanup
     * loops over `count`. */
    Py_ssize_t count = 0;
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
    /* Release the references taken when the ids were stashed. `Py_BuildValue`
     * took its own with the "O" format, so the returned list is unaffected -
     * and without this the kernel leaked exactly one reference per returned
     * candidate per call, which no differential test can see because every
     * answer is right and only the memory grows. */
    if (found) {
        for (Py_ssize_t i = 0; i < count; ++i) Py_XDECREF(found[i].id);
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

    /* Owned, not borrowed. `cells[0]` belongs to `row`, which is released at
     * the end of each iteration - safe today only because a list of lists
     * hands `PySequence_Fast` back the same object. Feed this a generator of
     * tuples and the borrowed pointer would outlive its owner. */
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
            Py_INCREF(cells[0]);
            Py_XDECREF(best_hit);
            best_hit = cells[0];
            best_along = along;
        }
        if (best_near == NULL || gap < best_gap) {
            Py_INCREF(cells[0]);
            Py_XDECREF(best_near);
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
    Py_XDECREF(best_hit);
    Py_XDECREF(best_near);

done:
    Py_XDECREF(fast);
    Py_XDECREF(max_o);
    Py_XDECREF(dir_o);
    Py_XDECREF(origin_o);
    Py_XDECREF(boxes);
    return result;
}

/* ---------------------------------------------------------------------- */
/* place_batch                                                            */
/* ---------------------------------------------------------------------- */
/*
 * Cull one cell's placements against the camera and pack what survived.
 *
 * The third seam kernel (D53), and its granularity is D26's argument applied
 * again: one call per resident cell per model per frame. What would otherwise
 * cross the boundary per placement - a world transform, a frustum verdict, a
 * matrix - never leaves C at all.
 *
 * Mirrors, branch for branch:
 *
 *   Camera.__post_init__   the orthonormalised basis and tan(fov/2)
 *   Camera.sees_sphere     four comparisons, in this order
 *   Transform.compose      quaternion product, then the rotated offset
 *   geometry.matrix4       the rotation matrix from that quaternion
 *   geometry.pack_matrix4  COLUMN-major, which is what GLSL wants
 *   ResidentCell.ambient_at  floor-divide, clamp, one byte, /255
 *
 * `lobster.conformance.AUTHORITY` is "python", so where this and the reference
 * disagree beyond the declared tolerance, THIS is wrong.
 */

typedef struct {
    Vec3 position, right, up, forward;
    double tan_x, tan_y, near, far;
} Camera;

typedef struct { double x, y, z, w; } Quat;

static Vec3 vcross(Vec3 a, Vec3 b)
{
    Vec3 r = { a.y*b.z - a.z*b.y, a.z*b.x - a.x*b.z, a.x*b.y - a.y*b.x };
    return r;
}

/* geometry.normalize: a zero vector comes back unchanged, not NaN. */
static Vec3 vnorm(Vec3 a)
{
    double len = vlen(a);
    if (len == 0.0) return a;
    Vec3 r = { a.x/len, a.y/len, a.z/len };
    return r;
}

/* geometry.quat_rotate */
static Vec3 quat_rotate(Quat q, Vec3 v)
{
    Vec3 u = { q.x, q.y, q.z };
    double s = q.w;
    double uv = vdot(u, v);
    double uu = vdot(u, u);
    Vec3 cr = vcross(u, v);
    Vec3 r = { 2.0*uv*u.x + (s*s - uu)*v.x + 2.0*s*cr.x,
               2.0*uv*u.y + (s*s - uu)*v.y + 2.0*s*cr.y,
               2.0*uv*u.z + (s*s - uu)*v.z + 2.0*s*cr.z };
    return r;
}

/* geometry.quat_mul */
static Quat quat_mul(Quat a, Quat b)
{
    Quat r = {
        a.w*b.x + a.x*b.w + a.y*b.z - a.z*b.y,
        a.w*b.y - a.x*b.z + a.y*b.w + a.z*b.x,
        a.w*b.z + a.x*b.y - a.y*b.x + a.z*b.w,
        a.w*b.w - a.x*b.x - a.y*b.y - a.z*b.z
    };
    return r;
}

static int read_quat(PyObject *seq, Quat *out)
{
    PyObject *fast = PySequence_Fast(seq, "expected a 4-element sequence");
    if (!fast) return 0;
    if (PySequence_Fast_GET_SIZE(fast) < 4) {
        Py_DECREF(fast);
        PyErr_SetString(PyExc_ValueError, "expected 4 quaternion components");
        return 0;
    }
    PyObject **items = PySequence_Fast_ITEMS(fast);
    double c[4];
    for (int i = 0; i < 4; ++i) {
        c[i] = PyFloat_AsDouble(items[i]);
        if (c[i] == -1.0 && PyErr_Occurred()) { Py_DECREF(fast); return 0; }
    }
    Py_DECREF(fast);
    out->x = c[0]; out->y = c[1]; out->z = c[2]; out->w = c[3];
    return 1;
}

/* One float out of a mapping, by key. Returns 0 with an exception set. */
static int read_double(PyObject *map, const char *key, double *out)
{
    PyObject *item = PyMapping_GetItemString(map, key);
    if (!item) return 0;
    *out = PyFloat_AsDouble(item);
    Py_DECREF(item);
    return !(*out == -1.0 && PyErr_Occurred());
}

static int read_vec3_key(PyObject *map, const char *key, Vec3 *out)
{
    PyObject *item = PyMapping_GetItemString(map, key);
    if (!item) return 0;
    int ok = read_vec3(item, out);
    Py_DECREF(item);
    return ok;
}

/* Camera.__post_init__, exactly: orthonormalise, then tan(fov/2). */
static int read_camera(PyObject *block, Camera *cam)
{
    double fov_y_deg, aspect;
    if (!read_vec3_key(block, "position", &cam->position)) return 0;
    if (!read_vec3_key(block, "forward", &cam->forward)) return 0;
    if (!read_vec3_key(block, "up", &cam->up)) return 0;
    if (!read_double(block, "fov_y_deg", &fov_y_deg)) return 0;
    if (!read_double(block, "aspect", &aspect)) return 0;
    if (!read_double(block, "near", &cam->near)) return 0;
    if (!read_double(block, "far", &cam->far)) return 0;

    Vec3 forward = vnorm(cam->forward);
    Vec3 right = vnorm(vcross(cam->up, forward));
    cam->right = right;
    cam->up = vnorm(vcross(forward, right));
    cam->forward = forward;

    double ty = tan(fov_y_deg * (M_PI / 180.0) * 0.5);
    cam->tan_y = ty;
    cam->tan_x = ty * aspect;
    return 1;
}

/* The baked lightmap, held as whatever the caller had. `bytes` is the runtime
 * shape and gets a direct pointer; a sequence is what a conformance vector
 * carries, and is read item by item. Both, because a native path with a fast
 * route the differential suite never exercises is a fast route nobody checked. */
typedef struct {
    const unsigned char *raw;   /* NULL when `seq` is in use */
    PyObject *seq;              /* borrowed */
    Py_ssize_t length;
    long side;
    double voxel;
} Lightmap;

static int read_lightmap(PyObject *payload, Lightmap *lm, PyObject **keepalive)
{
    lm->raw = NULL; lm->seq = NULL; lm->length = 0; lm->side = 0;
    lm->voxel = 1.0;
    *keepalive = NULL;

    PyObject *block = PyMapping_GetItemString(payload, "lightmap");
    if (!block) { PyErr_Clear(); return 1; }
    if (block == Py_None) { Py_DECREF(block); return 1; }

    PyObject *data = PyMapping_GetItemString(block, "data");
    PyObject *side_o = PyMapping_GetItemString(block, "side");
    PyObject *voxel_o = PyMapping_GetItemString(block, "voxel_size");
    int ok = 0;
    if (data && side_o) {
        lm->side = PyLong_AsLong(side_o);
        if (!(lm->side == -1 && PyErr_Occurred())) {
            if (voxel_o && voxel_o != Py_None) {
                lm->voxel = PyFloat_AsDouble(voxel_o);
                if (lm->voxel == -1.0 && PyErr_Occurred()) goto out;
            }
            if (lm->voxel == 0.0) lm->voxel = 1.0;
            if (PyBytes_Check(data)) {
                lm->raw = (const unsigned char *)PyBytes_AS_STRING(data);
                lm->length = PyBytes_GET_SIZE(data);
                Py_INCREF(data);
                *keepalive = data;      /* the pointer must outlive `data` */
            } else {
                PyObject *fast = PySequence_Fast(data, "lightmap data");
                if (!fast) goto out;
                lm->seq = fast;
                lm->length = PySequence_Fast_GET_SIZE(fast);
                *keepalive = fast;      /* owned; released by the caller */
            }
            ok = 1;
        }
    } else {
        PyErr_Clear();
        ok = 1;                          /* no data means "unlit", not an error */
    }
out:
    Py_XDECREF(voxel_o);
    Py_XDECREF(side_o);
    Py_XDECREF(data);
    Py_DECREF(block);
    return ok;
}

/* ResidentCell.ambient_at, over the bytes. Python's `//` floors towards
 * negative infinity and C's cast truncates towards zero, so a negative
 * coordinate needs `floor` or every point west of the origin lands one cell
 * too far east - and only on one side, which is the kind of asymmetry that
 * looks like a lighting artefact rather than a bug. */
static double sample_light(const Lightmap *lm, Vec3 point, int *failed)
{
    if (lm->length == 0 || lm->side <= 0) return 1.0;
    long ix = (long)floor(point.x / lm->voxel);
    long iz = (long)floor(point.z / lm->voxel);
    if (ix < 0) ix = 0; else if (ix >= lm->side) ix = lm->side - 1;
    if (iz < 0) iz = 0; else if (iz >= lm->side) iz = lm->side - 1;
    Py_ssize_t index = (Py_ssize_t)iz * lm->side + ix;
    if (index >= lm->length) return 1.0;
    if (lm->raw) return (double)lm->raw[index] / 255.0;
    PyObject **items = PySequence_Fast_ITEMS(lm->seq);
    long value = PyLong_AsLong(items[index]);
    if (value == -1 && PyErr_Occurred()) { *failed = 1; return 1.0; }
    return (double)value / 255.0;
}

static PyObject *place_batch(PyObject *self, PyObject *payload)
{
    PyObject *camera_o = NULL, *cell_o = NULL, *placements = NULL;
    PyObject *fast = NULL, *result = NULL, *visible = NULL, *centers = NULL;
    PyObject *distances = NULL, *instances = NULL, *lm_keep = NULL;
    float *packed = NULL;
    Camera cam;
    Lightmap lm;
    Vec3 cell_pos;
    Quat cell_rot;
    double radius;
    Py_ssize_t kept = 0;

    camera_o = PyMapping_GetItemString(payload, "camera");
    if (!camera_o || !read_camera(camera_o, &cam)) goto done;

    cell_o = PyMapping_GetItemString(payload, "cell");
    if (!cell_o) goto done;
    if (!read_vec3_key(cell_o, "position", &cell_pos)) goto done;
    {
        PyObject *rot = PyMapping_GetItemString(cell_o, "rotation");
        if (!rot) goto done;
        int ok = read_quat(rot, &cell_rot);
        Py_DECREF(rot);
        if (!ok) goto done;
    }
    if (!read_double(payload, "radius", &radius)) goto done;
    if (!read_lightmap(payload, &lm, &lm_keep)) goto done;

    placements = PyMapping_GetItemString(payload, "placements");
    if (!placements) goto done;
    fast = PySequence_Fast(placements, "placements must be a sequence");
    if (!fast) goto done;

    Py_ssize_t floats = PySequence_Fast_GET_SIZE(fast);
    if (floats % PLACEMENT_FLOATS != 0) {
        PyErr_Format(PyExc_ValueError,
                     "placements has %zd floats, which is not a whole number "
                     "of %d-float placements", floats, PLACEMENT_FLOATS);
        goto done;
    }
    Py_ssize_t total = floats / PLACEMENT_FLOATS;
    PyObject **cells = PySequence_Fast_ITEMS(fast);

    visible = PyList_New(0);
    centers = PyList_New(0);
    distances = PyList_New(0);
    if (!visible || !centers || !distances) goto done;
    packed = (float *)PyMem_Malloc((total ? total : 1)
                                   * INSTANCE_FLOATS * sizeof(float));
    if (!packed) { PyErr_NoMemory(); goto done; }

    for (Py_ssize_t i = 0; i < total; ++i) {
        double v[PLACEMENT_FLOATS];
        for (int k = 0; k < PLACEMENT_FLOATS; ++k) {
            v[k] = PyFloat_AsDouble(cells[i * PLACEMENT_FLOATS + k]);
            if (v[k] == -1.0 && PyErr_Occurred()) goto done;
        }
        Vec3 local_pos = { v[0], v[1], v[2] };
        Quat local_rot = { v[3], v[4], v[5], v[6] };

        /* Transform.compose: the outer rotation applied to the inner offset,
         * then the quaternion product. Order matters and is not symmetric. */
        Vec3 spun = quat_rotate(cell_rot, local_pos);
        Vec3 centre = { cell_pos.x + spun.x, cell_pos.y + spun.y,
                        cell_pos.z + spun.z };
        Quat rot = quat_mul(cell_rot, local_rot);

        /* Camera.sees_sphere, in its order. */
        Vec3 rel = vsub(centre, cam.position);
        double vx = vdot(rel, cam.right);
        double vy = vdot(rel, cam.up);
        double vz = vdot(rel, cam.forward);
        if (vz + radius < cam.near || vz - radius > cam.far) continue;
        double limit_x = vz * cam.tan_x
                         + radius * sqrt(1.0 + cam.tan_x * cam.tan_x);
        if (fabs(vx) > limit_x) continue;
        double limit_y = vz * cam.tan_y
                         + radius * sqrt(1.0 + cam.tan_y * cam.tan_y);
        if (fabs(vy) > limit_y) continue;

        PyObject *index_o = PyLong_FromSsize_t(i);
        if (!index_o || PyList_Append(visible, index_o) != 0) {
            Py_XDECREF(index_o); goto done;
        }
        Py_DECREF(index_o);

        PyObject *centre_o = Py_BuildValue("[ddd]", centre.x, centre.y,
                                           centre.z);
        if (!centre_o || PyList_Append(centers, centre_o) != 0) {
            Py_XDECREF(centre_o); goto done;
        }
        Py_DECREF(centre_o);

        PyObject *dist_o = PyFloat_FromDouble(vlen(rel));
        if (!dist_o || PyList_Append(distances, dist_o) != 0) {
            Py_XDECREF(dist_o); goto done;
        }
        Py_DECREF(dist_o);

        /* geometry.matrix4, written straight out in COLUMN-major order -
         * `pack_matrix4` transposes on the way out, so building the row-major
         * 4x4 first and then transposing would be two steps to arrive here. */
        double x = rot.x, y = rot.y, z = rot.z, w = rot.w;
        float *out = packed + kept * INSTANCE_FLOATS;
        out[0]  = (float)(1 - 2*(y*y + z*z));
        out[1]  = (float)(2*(x*y + z*w));
        out[2]  = (float)(2*(x*z - y*w));
        out[3]  = 0.0f;
        out[4]  = (float)(2*(x*y - z*w));
        out[5]  = (float)(1 - 2*(x*x + z*z));
        out[6]  = (float)(2*(y*z + x*w));
        out[7]  = 0.0f;
        out[8]  = (float)(2*(x*z + y*w));
        out[9]  = (float)(2*(y*z - x*w));
        out[10] = (float)(1 - 2*(x*x + y*y));
        out[11] = 0.0f;
        out[12] = (float)centre.x;
        out[13] = (float)centre.y;
        out[14] = (float)centre.z;
        out[15] = 1.0f;

        int failed = 0;
        out[16] = (float)sample_light(&lm, centre, &failed);
        if (failed) goto done;
        kept += 1;
    }

    instances = PyBytes_FromStringAndSize(
        (const char *)packed, kept * INSTANCE_FLOATS * (Py_ssize_t)sizeof(float));
    if (!instances) goto done;

    result = Py_BuildValue("{s:N,s:N,s:N,s:N}",
                           "visible", visible, "centers", centers,
                           "distances", distances, "instances", instances);
    if (result) { visible = centers = distances = instances = NULL; }

done:
    PyMem_Free(packed);
    Py_XDECREF(instances);
    Py_XDECREF(distances);
    Py_XDECREF(centers);
    Py_XDECREF(visible);
    Py_XDECREF(fast);
    Py_XDECREF(placements);
    Py_XDECREF(lm_keep);
    Py_XDECREF(cell_o);
    Py_XDECREF(camera_o);
    return result;
}

/* ---------------------------------------------------------------------- */

static PyMethodDef methods[] = {
    {"segment_query", segment_query, METH_O,
     "Entities within radius of a segment, nearest first."},
    {"nearest_region", nearest_region, METH_O,
     "Which bone a ray struck, over an already-filtered capsule list."},
    {"place_batch", place_batch, METH_O,
     "Cull one cell's placements and pack the instances that survived."},
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
