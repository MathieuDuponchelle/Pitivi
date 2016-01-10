from pkg_resources import iter_entry_points
from toposort import toposort_flatten


class BaseExtension(object):

    def __init__(self, app):
        self.app = app

    def setup(self):
        pass

    def finalize(self):
        pass

    @staticmethod
    def get_dependencies():
        return []


def get_all_extension_classes(sort):
    """
    Retrieves all the packages that registered an entry point.
    Optionally sort them so that extensions can specify other
    extensions they might depend upon.
    This doesn't instantiate the extensions themselves.
    """
    all_classes = {}
    deps_map = {}

    for entry_point in iter_entry_points(group='pitivi.extensions',
                                         name='get_extension_classes'):
        try:
            activation_function = entry_point.load()
            classes = activation_function()
        except Exception as e:
            print ("Failed to load %s" % entry_point.module_name, e)
            continue

        for klass in classes:
            all_classes[klass.EXTENSION_NAME] = klass

    if not sort:
        return all_classes

    for klass in all_classes.values():
        deps = klass.get_dependencies()
        satisfied = True
        topodeps = set()
        for dep in deps:
            if dep.dependency_name not in all_classes:
                print ("Missing dependency %s for %s" % (dep.dependency_name,
                                                         klass.EXTENSION_NAME))
                satisfied = False
                break
            if dep.upstream is True:
                topodeps.add(all_classes[dep.dependency_name])

        if not satisfied:
            continue

        deps_map[klass] = topodeps

    sorted_classes = toposort_flatten(deps_map)
    return sorted_classes
