import numpy as np
import math
import pymesh
import grid_mesh
from scipy.sparse.linalg import cg
from cons_model import Corotated
class efem:
    def __init__(self, config, grid, mesh, dirichlet_bc, dirichlet_mapping):
        self.config = config
        self.grid = grid
        self.deformed_grid = grid
        self.mesh = mesh
        self.incident_element = grid_mesh.incident_element(config, grid, mesh)
        self.faces = np.reshape(mesh, (-1,3))
        self.Ne = mesh.size//3
        self.F = np.zeros((self.Ne, config.d, config.d))
        self.dirchlet_bc = dirichlet_bc
        self.dirichlet_mapping = dirichlet_mapping
        self.initialize()
        self.initialize_nodalmass()
        self.initalize_interpolant_gradient()
        # self.dirchlet_pts
        # self.non_dirichlet_pts
        # self.nodalmass
        # self.M
        # self.num_inside_pts
    def initialize(self):
        """Initialize Dm, Dminv, Ds, vol, dirichlet_pts, non_dirichlet_pts"""
        N, d, npt = self.config.N, self.config.d, self.config.npt
        Ne = self.Ne
        vol = np.zeros(Ne)
        self.Dm = np.zeros((Ne,d,d))
        self.Dminv = np.zeros((Ne, d, d))
        self.Ds = np.zeros((Ne,d,d))
        self.vol = np.zeros(Ne)
        for e in range(Ne):
            index_list = []
            point_list = []
            vector_list = []
            for j in range(d+1):
                index = self.mesh[(d+1)*e+j]
                index_list.append(index)
                point_list.append(self.grid[index,:])
            for j in range(1,d+1):
                vector_list.append(point_list[j]-point_list[0])
            D_m = np.stack(vector_list).transpose()
            self.Dm[e,:,:] = D_m
            self.Dminv[e,:,:] = np.linalg.inv(D_m)
            self.vol[e] = (1/math.factorial(d))*abs(np.linalg.det(D_m))
        self.build_dirichlet_pts()
        self.map_dirichlet()
        self.updateDs_F()

    def build_dirichlet_pts(self):
        dirchlet_pts = []
        non_dirichlet_pts = []
        self.i_to_vectori = []
        vectori = 0
        for i in range(self.config.npt):
            x,y = self.grid[i,:]
            if self.dirchlet_bc(x,y):
                dirchlet_pts.append(i)
                self.i_to_vectori.append(None)
            else:
                non_dirichlet_pts.append(i)
                self.i_to_vectori.append(vectori)
                vectori += 1
        self.dirchlet_pts = dirchlet_pts
        self.non_dirichlet_pts = non_dirichlet_pts
        self.num_inside_pts = len(self.non_dirichlet_pts)

    def map_dirichlet(self):
        for i in self.dirchlet_pts:
            x,y = self.grid[i,:]
            self.deformed_grid[i,:] = self.dirichlet_mapping(x,y)
        x, y = self.grid[4, :]
        # self.deformed_grid[4, :] = [1,0.5]

    def initialize_nodalmass(self):
        """grid point i, nodalmass[i] = mass matrix M_ii, after mass lumping"""
        npt = self.config.npt
        d = self.config.d
        rho = self.config.rho
        self.nodalmass = np.zeros(npt)
        for e in range(self.mesh.size//(d+1)):
            for i in range(d+1):
                mass = self.vol[e]*rho/3
                self.nodalmass[self.mesh[(d+1)*e+i]] += mass

        self.M = np.zeros((d*len(self.non_dirichlet_pts),d*len(self.non_dirichlet_pts)))
        for i in range(self.num_inside_pts):
            index = self.non_dirichlet_pts[i]
            for j in range(d):
                self.M[i+j,i+j] = self.nodalmass[index]

    def initalize_interpolant_gradient(self):
        d = self.config.d
        self.grad_N = np.zeros((self.Ne * (d + 1), d))  # (len(mesh), 2)
        self.canonical_grad_N = np.array([[-1, -1], [1, 0], [0, 1]])
        for e in range(self.Ne):
            for alpha in range(d+1):
                self.grad_N[(d+1)*e+alpha] = self.Dminv[e].transpose().dot(self.canonical_grad_N[alpha])

    def run(self):
        d = self.config.d
        num_inside_pts = self.num_inside_pts
        phi_0 = np.zeros(d*num_inside_pts)
        for i in range(num_inside_pts):
            phi_0[i*d: (i+1)*d] = self.deformed_grid[self.non_dirichlet_pts[i], :]
        phi_1 = phi_0
        phi_2 = phi_0
        self.deformed_to_obj("frame_1.obj")
        for timestep in range(self.config.num_tpt):
            # given phi_(n-1), phi_n, find phi_(n+1) s.t. g(phi_(n+1)) = 0 using newton's method
            # g = lambda phi: self.M.dot(phi) - 2*self.M.dot(phi_1) + self.M.dot(phi_0) - dt*dt*self.external_force(phi)
            phi_1, phi_0 = self.advance_one_step(phi_0, phi_1), phi_1
            print(phi_1)
            self.deformed_to_obj(f"frame_{timestep+2}.obj")

    def advance_one_step(self, phi_0, phi_1, tol_newton = 2*10e-6, tol_cg = 10e-4):
        dt = self.config.dt
        g = lambda phi: self.M.dot(phi) - 2*self.M.dot(phi_1) + self.M.dot(phi_0) - dt*dt*self.internal_force()
        Dg = lambda phi: self.M - dt*dt*self.Df()
        phi = phi_1
        while np.linalg.norm(g(phi)) > tol_newton:
            self.update_phi(phi)
            self.updateDs_F()
            dphi, res = cg(Dg(phi), -g(phi))
            phi += dphi
            print(phi)
            print("BE energy =", self.BE_energy(phi, phi_1, phi_0))
            print("cg residual =", res)
            print("residual g =", np.linalg.norm(g(phi)), "internal force =", dt * dt * self.internal_force())
        print("after newton, BE energy =", self.BE_energy(phi, phi_1, phi_0))
        print("after newton, residual g =", np.linalg.norm(g(phi)))
        return phi

    def internal_force(self):
        """assembly external force vector based on current deformed_grid, Ds, F"""
        d, num_inside_pts = self.config.d, self.num_inside_pts
        f = np.zeros(d * num_inside_pts)
        mu, lambd = self.config.mu, self.config.lambd
        for e in range(self.Ne):
            model = Corotated(mu, lambd, self.F[e, :, :])
            P = model.P()
            for p in range(d + 1):
                i = self.mesh[(d+1)*e+p]
                for beta in range(d):
                    for gamma in range(d):
                        vectori = self.i_to_vectori[i]
                        if vectori != None:
                            f[d*vectori+beta] += -self.vol[e]*P[beta,gamma]*self.grad_N[(d+1)*e+p, gamma]
        return f

    def Df(self):
        d, num_inside_pts = self.config.d, self.num_inside_pts
        df_dphi = np.zeros((d * num_inside_pts, d * num_inside_pts))
        mu, lambd = self.config.mu, self.config.lambd
        for e in range(self.Ne):
            model = Corotated(mu, lambd, self.F[e, :, :])
            dPdF = model.dPdF()
            for p in range(d + 1):
                mesh_i = (d+1)*e+p
                i = self.mesh[mesh_i]
                for q in range(d+1):
                    mesh_j = (d+1)*e+q
                # for mesh_j in self.incident_element[i]:
                    j = self.mesh[mesh_j]
                    for beta in range(d):
                        for gamma in range(d):
                            for alpha in range(d):
                                for epsilon in range(d):
                                    vectori, vectorj = self.i_to_vectori[i],  self.i_to_vectori[j]
                                    if vectori != None and vectorj != None:
                                        df_dphi[vectori*d+beta, vectorj*d+alpha] += \
                                            -self.vol[e]*dPdF[beta*d+gamma, alpha*d+epsilon]*self.grad_N[mesh_j,epsilon]*self.grad_N[mesh_i,gamma]
        return df_dphi

    def update_phi(self, phi):
        """updates self.deformed_grid based on phi"""
        for i in range(self.num_inside_pts):
            index = self.non_dirichlet_pts[i]
            for beta in range(self.config.d):
                self.deformed_grid[index, beta] = phi[self.config.d*i+beta]

    def BE_energy(self, phi, phi1, phi0):
        return 1/2*phi.transpose().dot(self.M).dot(phi)+ phi.transpose().dot(-2*self.M.dot(phi1)+self.M.dot(phi0)) + self.config.dt**2*self.energy()

    def energy(self):
        ans = 0
        for e in range(self.Ne):
            model = Corotated(self.config.mu, self.config.lambd, self.F[e,:,:])
            ans += model.psi()
        return ans

    def updateDs_F(self):
        d = self.config.d
        for e in range(self.Ne):
            if d == 2:
                i0,i1,i2 = self.mesh[(d+1)*e:(d+1)*e+(d+1)]
                self.Ds[e,:,:] = np.stack([self.deformed_grid[i1]-self.deformed_grid[i0], self.deformed_grid[i2]-self.deformed_grid[i0]]).transpose()

        # F = Ds*Dminv
        for e in range(self.Ne):
            self.F[e] = np.dot(self.Ds[e],self.Dminv[e])

    def deformed_to_obj(self, filename):
        deformed = np.append(self.deformed_grid, np.zeros((self.config.npt,1)), 1)

        pymesh.save_mesh(filename, pymesh.form_mesh(deformed, self.faces))
