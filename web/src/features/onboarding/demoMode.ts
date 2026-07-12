import { createContext, useContext } from 'react';

export const OnboardingDemoContext = createContext(false);

export function useOnboardingDemoMode(): boolean {
  return useContext(OnboardingDemoContext);
}
