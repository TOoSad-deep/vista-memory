// Preserve the old local installer API; ShaderService now calls afterPublish itself.
import { candidateRegistration as createRegistration } from '../../dsh-shader-11-13/src/vista-registration.js';

export function candidateRegistration(service, bridge, vistaRoot, sourceAgent) {
  service.vistaRoot = vistaRoot;
  service.vistaBridge = bridge;
  service.vistaRegistration = createRegistration(service, bridge, vistaRoot, sourceAgent);
  return service.vistaRegistration;
}
