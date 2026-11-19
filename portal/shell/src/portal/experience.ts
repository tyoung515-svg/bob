import { createContext, useContext } from 'react';
export const ExperienceContext = createContext<'simple' | 'pro'>('pro');
export const useExperience = () => useContext(ExperienceContext);
