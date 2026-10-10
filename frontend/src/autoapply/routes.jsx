import React from 'react'
import { Route } from 'react-router-dom'
import AutoApplyQueue from './AutoApplyQueue'
import AutoApplyStats from './AutoApplyStats'

// Mounted under the app Shell by a single hook line in App.jsx.
export const autoApplyRoutes = () => (
  <>
    <Route path="autoapply" element={<AutoApplyQueue />} />
    <Route path="autoapply/stats" element={<AutoApplyStats />} />
  </>
)
