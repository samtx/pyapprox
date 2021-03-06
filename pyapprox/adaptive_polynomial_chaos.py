import numpy as np
from pyapprox.multivariate_polynomials import PolynomialChaosExpansion, \
    define_poly_options_from_variable_transformation
from pyapprox.adaptive_sparse_grid import SubSpaceRefinementManager
from pyapprox.induced_sampling import increment_induced_samples_migliorati, \
    generate_induced_samples_migliorati_tolerance, christoffel_weights
from scipy.linalg import solve_triangular
from pyapprox.utilities import add_columns_to_pivoted_lu_factorization, \
    continue_pivoted_lu_factorization, get_final_pivots_from_sequential_pivots,\
    split_lu_factorization_matrix, pivot_rows, hash_array, \
    truncated_pivoted_lu_factorization, unprecondition_LU_factor
from pyapprox.probability_measure_sampling import generate_independent_random_samples
def get_subspace_active_poly_array_indices(adaptive_pce,ii):
    idx1=adaptive_pce.unique_poly_indices_idx[ii]
    if ii < adaptive_pce.unique_poly_indices_idx.shape[0]-1:
        idx2=adaptive_pce.unique_poly_indices_idx[ii+1]
    else:
        idx2=adaptive_pce.poly_indices.shape[1]
    return np.arange(idx1,idx2)

def get_active_poly_array_indices(adaptive_pce):
    indices = np.empty((0),dtype=int)
    for key,ii in adaptive_pce.active_subspace_indices_dict.items():
        subspace_array_indices = get_subspace_active_poly_array_indices(
            adaptive_pce,ii)
        indices = np.hstack([indices,subspace_array_indices])
    return indices

def variance_pce_refinement_indicator(
        subspace_index,num_new_subspace_samples,adaptive_pce,
        normalize=True,mean_only=False):
    """
    Set pce coefficients of new subspace poly indices to zero to compute
    previous mean then set them to be non-zero
    """
    key = hash_array(subspace_index)
    ii = adaptive_pce.active_subspace_indices_dict[key]
    I = get_subspace_active_poly_array_indices(adaptive_pce,ii)
    error = np.sum(adaptive_pce.pce.coefficients[I]**2,axis=0)
    indicator=error.copy()
    #print(subspace_index,error)
                
    # relative error will not work if value at first grid point is close to zero
    if normalize:
        assert np.all(np.absolute(adaptive_pce.values[0,:])>1e-6)
        indicator/=np.absolute(adaptive_pce.values[0,:])**2

    qoi_chosen = np.argmax(indicator)

    indicator=indicator.max()

    cost_per_sample = adaptive_pce.eval_cost_function(
        subspace_index[:,np.newaxis])
    cost = cost_per_sample*num_new_subspace_samples

    # compute marginal benefit
    indicator/=cost
    #print(subspace_index,indicator,'indicator')
    return -indicator, error[qoi_chosen]
    
def solve_preconditioned_least_squares(basis_matrix_func,samples,values,
                                       precond_func):
    basis_matrix = basis_matrix_func(samples)
    #weights = np.sqrt(basis_matrix.shape[1]*christoffel_weights(basis_matrix))
    weights = precond_func(basis_matrix,samples)
    basis_matrix = basis_matrix*weights[:,np.newaxis]
    rhs = values*weights[:,np.newaxis]
    #print(np.linalg.cond(basis_matrix))
    coef = np.linalg.lstsq(basis_matrix,rhs,rcond=None)[0]
    return coef

def solve_preconditioned_orthogonal_matching_pursuit(basis_matrix_func,
                                                     samples,values,
                                                     precond_func,
                                                     tol=1e-8):
    from sklearn.linear_model import OrthogonalMatchingPursuit
    basis_matrix = basis_matrix_func(samples)
    weights = precond_func(basis_matrix,samples)
    basis_matrix = basis_matrix*weights[:,np.newaxis]
    rhs = values*weights[:,np.newaxis]
    omp = OrthogonalMatchingPursuit(tol=tol,fit_intercept=False)
    omp.fit(basis_matrix, rhs)
    coef = omp.coef_
    print('nnz_terms',np.count_nonzero(coef))
    return coef[:,np.newaxis]

def chistoffel_preconditioning_function(basis_matrix,samples):
    weights = np.sqrt(basis_matrix.shape[1]*christoffel_weights(basis_matrix))
    return weights

class AdaptiveInducedPCE(SubSpaceRefinementManager):
    def __init__(self,num_vars,cond_tol=1e2):
        super(AdaptiveInducedPCE,self).__init__(num_vars)
        self.cond_tol=cond_tol
        self.fit_opts = {'omp_tol':0}
        self.set_preconditioning_function(chistoffel_preconditioning_function)
        self.fit_function = self._fit
        if cond_tol<1:
            self.induced_sampling=False
            self.set_preconditioning_function(precond_func = lambda m,x: np.ones(x.shape[1]))
            self.sample_ratio=5
        else:
            self.induced_sampling=True
            self.sample_ratio=None

    def set_function(self,function,var_trans=None,pce=None):
        super(AdaptiveInducedPCE,self).set_function(function,var_trans)
        self.set_polynomial_chaos_expansion(pce)

    def set_polynomial_chaos_expansion(self,pce=None):
        if pce is None:
            poly_opts=define_poly_options_from_variable_transformation(
                self.variable_transformation)
            self.pce = PolynomialChaosExpansion()
            self.pce.configure(poly_opts)
        else:
            self.pce=pce

    def increment_samples(self,current_poly_indices,unique_poly_indices):
        if self.induced_sampling:
            samples = increment_induced_samples_migliorati(
                self.pce,self.cond_tol,self.samples,
                current_poly_indices, unique_poly_indices)
        else:
            samples = generate_independent_random_samples(self.pce.var_trans.variable,self.sample_ratio*unique_poly_indices.shape[1])
            samples = self.pce.var_trans.map_to_canonical_space(samples)
            samples = np.hstack([self.samples,samples])
        return samples

    def allocate_initial_samples(self):
        if self.induced_sampling:
            return generate_induced_samples_migliorati_tolerance(self.pce,self.cond_tol)
        else:
            return generate_independent_random_samples(self.pce.var_trans.variable,self.sample_ratio*self.pce.num_terms())
        
    def create_new_subspaces_data(self,new_subspace_indices):
        num_current_subspaces = self.subspace_indices.shape[1]
        self.initialize_subspaces(new_subspace_indices)

        self.pce.set_indices(self.poly_indices)
        if self.samples.shape[1]==0:
            unique_subspace_samples = self.allocate_initial_samples()
            return unique_subspace_samples, np.array(
                [unique_subspace_samples.shape[1]])

        num_vars, num_new_subspaces = new_subspace_indices.shape
        unique_poly_indices = np.zeros((num_vars,0),dtype=int)
        for ii in range(num_new_subspaces):
            I = get_subspace_active_poly_array_indices(
                self,num_current_subspaces+ii)
            unique_poly_indices = np.hstack(
                [unique_poly_indices,self.poly_indices[:,I]])

        # current_poly_indices will include active indices not added
        # during this call, i.e. in new_subspace_indices.
        # thus cannot use
        # I = get_active_poly_array_indices(self)
        # unique_poly_indices = self.poly_indices[:,I]
        # to replace above loop
        current_poly_indices = self.poly_indices[
            :,:self.unique_poly_indices_idx[num_current_subspaces]]
        num_samples = self.samples.shape[1]
        samples = self.increment_samples(current_poly_indices,unique_poly_indices)
        unique_subspace_samples = samples[:,num_samples:]

        # warning num_new_subspace_samples does not really make sense for
        # induced sampling as new samples are not directly tied to newly
        # added basis
        num_new_subspace_samples = unique_subspace_samples.shape[1]*np.ones(
            new_subspace_indices.shape[1])//new_subspace_indices.shape[1]
        return unique_subspace_samples, num_new_subspace_samples

    def _fit(self,pce,canonical_basis_matrix,samples,values,
             precond_func=None,omp_tol=0):
        # do to, just add columns to stored basis matrix
        # store qr factorization of basis_matrix and update the factorization
        # self.samples are in canonical domain
        if omp_tol==0:
            coef = solve_preconditioned_least_squares(
                canonical_basis_matrix,samples,values,precond_func)
        else:
            coef = solve_preconditioned_orthogonal_matching_pursuit(
                canonical_basis_matrix,samples,values,precond_func,omp_tol)
        self.pce.set_coefficients(coef)
        
    def fit(self):
        return self.fit_function(
            self.pce,self.pce.canonical_basis_matrix,self.samples,self.values,
            **self.fit_opts)

    def add_new_subspaces(self,new_subspace_indices):
        num_new_subspaces = new_subspace_indices.shape[1]
        num_current_subspaces = self.subspace_indices.shape[1]
        num_new_subspace_samples = super(
            AdaptiveInducedPCE,self).add_new_subspaces(new_subspace_indices)

        self.fit()

        return num_new_subspace_samples

    def __call__(self,samples):
        return self.pce(samples)

    def get_active_unique_poly_indices(self):
        I = get_active_poly_array_indices(self)
        return self.poly_indices[:,I]

    def set_preconditioning_function(self,precond_func):
        """
        precond_func : callable
            Callable function with signature precond_func(basis_matrix,samples)
        """
        self.precond_func=precond_func
        self.fit_opts['precond_func']=self.precond_func

class AdaptiveLejaPCE(AdaptiveInducedPCE):
    def __init__(self,num_vars,candidate_samples,factorization_type='fast'):
        #todo remove cond_tol from __init__
        super(AdaptiveLejaPCE,self).__init__(num_vars,1e-8)
        # must be in canonical space
        # TODO: generate candidate samples at each iteration from induced
        # distribution using current self.poly_indices
        self.candidate_samples = candidate_samples
        self.factorization_type = factorization_type

    def precond_canonical_basis_matrix(self,samples):
        basis_matrix = self.pce.canonical_basis_matrix(samples)
        #precond_weights=np.sqrt(basis_matrix.shape[1])/np.linalg.norm(
        #    basis_matrix,axis=1)
        precond_weights=self.precond_func(basis_matrix,samples)
        precond_basis_matrix = basis_matrix*precond_weights[:,np.newaxis]
        return precond_basis_matrix, precond_weights

    def get_num_new_subspace_samples(self,new_subspace_indices,
                                     num_current_subspaces):
        num_current_subspaces = self.subspace_indices.shape[1]
        num_vars, num_new_subspaces = new_subspace_indices.shape
        unique_poly_indices = np.zeros((num_vars,0),dtype=int)
        num_new_subspace_samples=np.empty((num_new_subspaces),dtype=int)
        for ii in range(num_new_subspaces):
            I = get_subspace_active_poly_array_indices(
                self,num_current_subspaces+ii)
            num_new_subspace_samples[ii] = I.shape[0]
        return num_new_subspace_samples

    def condition_number(self):
        if self.factorization_type=='slow':
            return np.linalg.cond(self.L_factor.dot(self.U_factor))
        else:
            L,U = split_lu_factorization_matrix(
                self.LU_factor,num_pivots=self.samples.shape[1])
            return np.linalg.cond(L.dot(U))

    def update_leja_sequence_slow(self,new_subspace_indices):
        num_samples = self.samples.shape[1]
        # There will be two copies of self.samples in candidate_samples
        # but pivoting will only choose these samples once when number of
        # desired samples is smaller than
        # self.candidate_samples.shape[0]-self.samples.shape[1]
        candidate_samples = np.hstack([self.samples,self.candidate_samples])

        self.pce.set_indices(self.poly_indices)
        precond_basis_matrix, precond_weights = \
            self.precond_canonical_basis_matrix(candidate_samples)

        # TODO: update LU factorization using new candidate points, This
        # requires writing a function that updates not just new columns of
        # L and U factor but also allows new rows to be added.
        max_iters = self.poly_indices.shape[1]
        num_initial_rows = num_samples
        self.L_factor,self.U_factor,pivots=\
          truncated_pivoted_lu_factorization(
              precond_basis_matrix,max_iters,num_initial_rows=num_initial_rows)
        self.pivots = np.arange(num_samples)[pivots[:num_initial_rows]]
        self.pivots = np.concatenate(
            [self.pivots,np.arange(num_initial_rows,pivots.shape[0])])
        self.precond_weights = precond_weights[pivots,np.newaxis]
        return candidate_samples[:,pivots[num_samples:]]

    def update_leja_sequence_fast(self,new_subspace_indices,
                                  num_current_subspaces):
        num_samples = self.samples.shape[1]
        if num_samples==0:
            self.pce.set_indices(self.poly_indices)
            max_iters = self.poly_indices.shape[1]
            # keep unconditioned
            self.basis_matrix = self.precond_canonical_basis_matrix(
                self.candidate_samples)[0]
            self.LU_factor,self.seq_pivots = \
                truncated_pivoted_lu_factorization(
                    self.basis_matrix, max_iters, truncate_L_factor=False)
            self.pivots=get_final_pivots_from_sequential_pivots(
                self.seq_pivots.copy())[:max_iters]
            #self.precond_weights = np.sqrt(
            #    self.basis_matrix.shape[1]*christoffel_weights(
            #        self.basis_matrix))[:,np.newaxis]
            self.precond_weights = self.precond_func(
                self.basis_matrix,self.candidate_samples)[:,np.newaxis]
            return self.candidate_samples[
                :,self.pivots[num_samples:self.poly_indices.shape[1]]]

        num_vars, num_new_subspaces = new_subspace_indices.shape
        unique_poly_indices = np.zeros((num_vars,0),dtype=int)
        for ii in range(num_new_subspaces):
            I = get_subspace_active_poly_array_indices(
                self, num_current_subspaces+ii)
            unique_poly_indices = np.hstack(
                [unique_poly_indices, self.poly_indices[:,I]])
        self.pce.set_indices(unique_poly_indices)

        precond_weights_prev = self.precond_weights
        pivoted_precond_weights_prev = pivot_rows(
            self.seq_pivots, precond_weights_prev, False)
        
        new_cols = self.pce.canonical_basis_matrix(self.candidate_samples)
        self.basis_matrix = np.hstack([self.basis_matrix, new_cols.copy()])
        new_cols *= precond_weights_prev
        self.LU_factor = add_columns_to_pivoted_lu_factorization(
            self.LU_factor.copy(), new_cols, self.seq_pivots[:num_samples])
        
        #self.precond_weights = np.sqrt(
        #    self.basis_matrix.shape[1]*christoffel_weights(
        #        self.basis_matrix))[:,np.newaxis]
        self.precond_weights = self.precond_func(
            self.basis_matrix, self.candidate_samples)[:, np.newaxis]
        pivoted_precond_weights = pivot_rows(
            self.seq_pivots, self.precond_weights,False)
        self.LU_factor = unprecondition_LU_factor(
            self.LU_factor, pivoted_precond_weights_prev/pivoted_precond_weights,
            num_samples)
        
        it = self.poly_indices.shape[1]
        max_iters = self.poly_indices.shape[1]
        self.LU_factor, self.seq_pivots, it = continue_pivoted_lu_factorization(
            self.LU_factor.copy(), self.seq_pivots, self.samples.shape[1],
            max_iters,num_initial_rows=0)
        self.pivots=get_final_pivots_from_sequential_pivots(
            self.seq_pivots.copy())[:max_iters]

        self.pce.set_indices(self.poly_indices)
        return self.candidate_samples[
            :, self.pivots[num_samples:self.poly_indices.shape[1]]]

    def create_new_subspaces_data(self, new_subspace_indices):
        num_current_subspaces = self.subspace_indices.shape[1]
        self.initialize_subspaces(new_subspace_indices)

        if self.factorization_type=='fast':
            unique_subspace_samples = self.update_leja_sequence_fast(
                new_subspace_indices, num_current_subspaces)
        else:
            unique_subspace_samples = self.update_leja_sequence_slow(
                new_subspace_indices)

        num_new_subspace_samples = self.get_num_new_subspace_samples(
            new_subspace_indices, num_current_subspaces)
        return unique_subspace_samples, num_new_subspace_samples

    def add_new_subspaces(self,new_subspace_indices):
        num_new_subspaces = new_subspace_indices.shape[1]
        num_current_subspaces = self.subspace_indices.shape[1]
        num_new_subspace_samples = super(
            AdaptiveInducedPCE,self).add_new_subspaces(new_subspace_indices)

        if self.factorization_type == 'fast':
            it = self.samples.shape[1]
            temp = solve_triangular(
                self.LU_factor[:it, :it],
                self.values*self.precond_weights[self.pivots],
                lower=True, unit_diagonal=True)
            coef = solve_triangular(self.LU_factor[:it, :it], temp, lower=False)
            
        else:
            temp = solve_triangular(
                self.L_factor,
                self.values[self.pivots]*self.precond_weights,
                lower=True)
            coef = solve_triangular(self.U_factor, temp, lower=False)
        self.pce.set_coefficients(coef)

        return num_new_subspace_samples

    def __call__(self,samples):
        return self.pce(samples)

    def get_active_unique_poly_indices(self):
        I = get_active_poly_array_indices(self)
        return self.poly_indices[:,I]

    def build(self, callback=None):
        """
        """
        while (not self.active_subspace_queue.empty() or
               self.subspace_indices.shape[1] == 0):
            self.refine()
            self.recompute_active_subspace_priorities()
            
            if callback is not None:
                callback(self)
