
from scipy.spatial import KDTree
import numpy as np
cimport numpy as cnp
import matplotlib.pyplot as plt


cdef class TriangularMeshInterpolator2D(Function2D):
    """
    An abstract data structure for interpolating data points lying on a triangular mesh.
    """

    def __init__(self, vertex_coords, vertex_data, triangles, kdtree_search=True):
        """
        :param ndarray vertex_coords: An array of vertex coordinates with shape (num of vertices, 2). For each vertex
        there must be a (u, v) coordinate.
        :param ndarray vertex_data: An array of data points at each vertex with shape (num of vertices).
        :param ndarray triangles: An array of triangles with shape (num of triangles, 3). For each triangle, there must
        be three indicies that identify the three corresponding vertices in vertex_coords that make up this triangle.
        """

        self.vertices = np.zeros((vertex_coords.shape[0]), dtype=object)
        for index, vertex in enumerate(vertex_coords):
            self.vertices[index] = _TriangleMeshVertex(vertex[0], vertex[1], index)

        self.vertex_data = np.array(vertex_data, dtype=np.float64)

        self.triangles = np.zeros((triangles.shape[0]), dtype=object)
        for i, triangle in enumerate(triangles):
            try:
                v1 = _TriangleMeshVertex.all_vertices[triangle[0]]
                v2 = _TriangleMeshVertex.all_vertices[triangle[1]]
                v3 = _TriangleMeshVertex.all_vertices[triangle[2]]
            except IndexError:
                raise ValueError("vertex could not be found in vertex list")

            # proper kd-tree for triangles will make this unnecessary, vertices don't need to store triangle refs.
            triangle = _TriangleMeshTriangle(v1, v2, v3)
            v1.triangles.append(triangle)
            v2.triangles.append(triangle)
            v3.triangles.append(triangle)

            self.triangles[i] = triangle

        unique_vertices = [(vertex.u, vertex.v) for vertex in _TriangleMeshVertex.all_vertices]

        # TODO - implement KD-tree here
        # construct KD-tree from vertices
        self.kdtree = KDTree(unique_vertices)
        self.kdtree_search = kdtree_search

    def __call__(self, double x, double y):
        return self.evaluate(x, y)

    cdef double evaluate(self, double x, double y) except *:
        cdef _TriangleMeshTriangle triangle
        triangle = self.find_triangle_containing(x, y)
        if triangle:
            return triangle.evaluate(x, y, self)
        return 0.0

    cpdef _TriangleMeshTriangle find_triangle_containing(self, double u, double v):
        if self.kdtree_search:
            return self.kdtree_method(u, v)
        else:
            return self.brute_force_method(u, v)

    cdef _TriangleMeshTriangle brute_force_method(self, double u, double v):
        cdef _TriangleMeshTriangle triangle
        for triangle in self.triangles:
            if triangle.contains(u, v):
                return triangle
        return None

    cdef _TriangleMeshTriangle kdtree_method(self, double u, double v):
        cdef:
            long[:] i_closest
            double[:] dist
            _TriangleMeshVertex closest_vertex
            _TriangleMeshTriangle triangle

        # Find closest vertex through KD-tree
        dist, i_closest = self.kdtree.query((u, v), k=10)
        closest_vertex = self.vertices[i_closest[0]]

        # cycle through all triangles connected to this vertex
        for triangle in closest_vertex.triangles:
            if triangle.contains(u, v):
                return triangle
        return None

    cdef double get_vertex_data(self, int vertex_index):
        return self.vertex_data[vertex_index]

    def plot_mesh(self):
        plt.figure()
        for triangle in self.triangles:
            v1 = triangle.v1
            v2 = triangle.v2
            v3 = triangle.v3
            plt.plot([v1.u, v2.u, v3.u, v1.u], [v1.v, v2.v, v3.v, v1.v], color='b')

        plt.axis('equal')
        plt.show()

    def copy_mesh_with_new_data(self, vertex_data):
        """
        Make a new TriangularMeshInterpolator2D from an existing instance, but with new vertex data.

        :param ndarray vertex_data: An array of data points at each vertex with shape (num of vertices).
        """

        # Make a new mesh object without invoking __init__()
        new_mesh = TriangularMeshInterpolator2D.__new__(TriangularMeshInterpolator2D)

        # Copy over vertex data
        new_mesh.vertex_data = np.array(vertex_data, dtype=np.float64)

        # Copy over other mesh attributes
        new_mesh.vertices = self.vertices
        new_mesh.triangles = self.triangles
        new_mesh.kdtree = self.kdtree
        new_mesh.kdtree_search = self.kdtree_search
        return new_mesh


cdef class _TriangleMeshVertex:
    """
    An individual vertex of the mesh.
    """
    all_vertices = []

    def __init__(self, u, v, index):

        self.u = u
        self.v = v
        self.index = index
        self.triangles = []

        _TriangleMeshVertex.all_vertices.append(self)

    def __iter__(self):
        for tri in self.triangles:
            yield(tri)

    def __repr__(self):
        repr_str = "_TriangleMeshVertex => ({}, {})".format(self.u, self.v)
        return repr_str


cdef class _TriangleMeshTriangle:

    def __init__(self, v1, v2, v3):
        self.v1 = v1
        self.v2 = v2
        self.v3 = v3

    def __iter__(self):
        yield(self.v1)
        yield(self.v2)
        yield(self.v3)

    def __repr__(self):
        repr_str = "v1 => ({}, {})\n".format(self.v1.u, self.v1.v)
        repr_str += "v2 => ({}, {})\n".format(self.v2.u, self.v2.v)
        repr_str += "v3 => ({}, {})".format(self.v3.u, self.v3.v)
        return repr_str

    cdef double evaluate(self, double x, double y, TriangularMeshInterpolator2D mesh):

        cdef:
            double alpha, beta, gamma, alpha_data, beta_data, gamma_data
            _TriangleMeshVertex v1, v2, v3

        v1 = self.v1
        v2 = self.v2
        v3 = self.v3

        alpha_data = mesh.get_vertex_data(v1.index)
        beta_data = mesh.get_vertex_data(v2.index)
        gamma_data = mesh.get_vertex_data(v3.index)

        alpha = ((v2.v - v3.v)*(x - v3.u) + (v3.u - v2.u)*(y - v3.v)) / \
                ((v2.v - v3.v)*(v1.u - v3.u) + (v3.u - v2.u)*(v1.v - v3.v))
        beta = ((v3.v - v1.v)*(x - v3.u) + (v1.u - v3.u)*(y - v3.v)) /\
               ((v2.v - v3.v)*(v1.u - v3.u) + (v3.u - v2.u)*(v1.v - v3.v))
        gamma = 1.0 - alpha - beta

        return alpha * alpha_data + beta * beta_data + gamma * gamma_data

    cdef bint contains(self, double px, double py):
        """
        Test if a 2D point lies inside this triangle.

        Covert

        :param Point2D point: The point of interest
        :return True or False.
        """
        cdef:
            double alpha, beta, gamma
            _TriangleMeshVertex v1, v2, v3

        v1 = self.v1
        v2 = self.v2
        v3 = self.v3

        # Compute barycentric coordinates
        alpha = ((v2.v - v3.v)*(px - v3.u) + (v3.u - v2.u)*(py - v3.v)) / \
                ((v2.v - v3.v)*(v1.u - v3.u) + (v3.u - v2.u)*(v1.v - v3.v))
        beta = ((v3.v - v1.v)*(px - v3.u) + (v1.u - v3.u)*(py - v3.v)) /\
               ((v2.v - v3.v)*(v1.u - v3.u) + (v3.u - v2.u)*(v1.v - v3.v))
        gamma = 1.0 - alpha - beta

        # Point is inside triangle if all coordinates between [0, 1]
        if alpha > 0 and beta > 0 and gamma > 0:
            return True
        else:
            return False
